from __future__ import annotations

import fcntl
import hashlib
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.main import app, get_cups_client, get_file_storage
from app.services.file_storage import TempFileStorage
from app.services.office_conversion import ConversionError, OfficeConverter
from app.services.office_formats import OFFICE_FORMATS, OfficeFormatError, inspect_office
from tests.helpers import signup_user
from tests.office_fixtures import office_document
from tests.test_files_api import make_pdf
from tests.test_print_api import FakeCupsClient


@pytest.mark.parametrize('extension', OFFICE_FORMATS)
def test_inspection_matches_content(tmp_path, extension):
    source = tmp_path / 'upload'
    source.write_bytes(office_document(extension))
    assert inspect_office(source, f'file.{extension}') == extension
    assert inspect_office(source, f'file.{extension.upper()}') == extension
    with pytest.raises(OfficeFormatError):
        inspect_office(source, 'file.zip')
    with pytest.raises(OfficeFormatError):
        inspect_office(source, 'file.xlsx' if extension != 'xlsx' else 'file.docx')


@pytest.mark.parametrize('name,content', [
    ('word/vbaProject.bin', b'macro'), ('word/embeddings/object.bin', b'object'),
    ('../escape', b'x'), ('bad.xml', b'<!DOCTYPE x [<!ENTITY x "y">]><x/>'),
    ('word/_rels/bad.xml.rels', b'<Relationships><Relationship TargetMode="External" Type="image" Target="file:///etc/passwd"/></Relationships>'),
    ('xl/externalLinks/externalLink1.xml', b'<x/>'),
])
def test_unsafe_documents_rejected(tmp_path, name, content):
    buffer=BytesIO(office_document('docx'))
    with ZipFile(buffer, 'a') as archive:
        archive.writestr(name, content)
    source=tmp_path/'upload'
    source.write_bytes(buffer.getvalue())
    with pytest.raises(OfficeFormatError):
        inspect_office(source, 'file.docx')


@pytest.mark.parametrize('object_directory',['Object 2','Object 17','ObjectCustom'])
def test_odf_embedded_object_directories_rejected(tmp_path,object_directory):
    buffer=BytesIO(office_document('odt'))
    with ZipFile(buffer,'a') as archive:
        archive.writestr(f'{object_directory}/content.xml','<object/>')
    source=tmp_path/'file.odt';source.write_bytes(buffer.getvalue())
    with pytest.raises(OfficeFormatError,match='embedded objects'):
        inspect_office(source,source.name)


@pytest.fixture
def office_client(tmp_path, monkeypatch):
    storage = TempFileStorage(tmp_path / 'storage')
    calls=[]
    def convert(self, source, extension, destination):
        calls.append(extension)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(make_pdf(3))
        return 3
    monkeypatch.setattr(OfficeConverter, 'convert', convert)
    cups=FakeCupsClient()
    app.dependency_overrides[get_file_storage]=lambda: storage
    app.dependency_overrides[get_cups_client]=lambda: cups
    try:
        with TestClient(app) as client:
            signup_user(client)
            yield client, storage, cups, calls
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize('extension', OFFICE_FORMATS)
def test_office_api_reuses_pdf_for_preview_and_print(office_client, extension, monkeypatch):
    client, storage, cups, calls=office_client
    uploaded=client.post('/files', files={'file':(f'input.{extension}', office_document(extension), 'text/plain')})
    assert uploaded.status_code==200, uploaded.text
    info=uploaded.json()
    assert info['converted'] and info['preview_available']
    assert info['detected_mime']==OFFICE_FORMATS[extension][0]
    assert info['page_count']==3
    assert info['printable_mime']=='application/pdf'
    first=client.get(info['pdf_url'])
    assert first.status_code==200
    assert hashlib.sha256(first.content).hexdigest()==info['printable_sha256']
    assert client.get(info['pdf_url']).content==first.content
    assert client.get(f"/files/{info['file_id']}").json()==info
    assert len(client.get(f"/files/{info['file_id']}/preview").json()['pages'])==3
    request={'file_id':info['file_id'], 'options':{'pages':'3,1', 'paper_size':'A4'}}
    before=client.post('/print/validate', json=request)
    assert before.status_code==200
    assert before.json()['valid'] and before.json()['selected_pages']==[3,1]
    assert cups.submissions==[]
    def submit(path, title, options):
        assert len(PdfReader(path).pages)==2
        return 123
    monkeypatch.setattr(cups,'print_file',submit)
    printed=client.post('/print',json=request)
    assert printed.status_code==200, printed.text
    assert printed.json()['applied_options']==before.json()['applied_options']
    assert calls==[extension]
    assert not list(storage.filtered_dir.glob('*.pdf'))
    assert client.get('/history').json()['history'][0]['detected_mime']==info['detected_mime']
    client.post('/auth/logout')
    assert client.get(info['pdf_url']).status_code==401
    signup_user(client,'bob')
    assert client.get(info['pdf_url']).status_code==404
    assert client.get(f"/files/{info['file_id']}").status_code==404


@pytest.mark.parametrize('extension', OFFICE_FORMATS)
def test_long_office_filename_preserves_extension(office_client,extension):
    client,_,_,calls=office_client
    filename=f"{'long-name-' * 25}.{extension}"
    response=client.post('/files',files={'file':(filename,office_document(extension))})
    assert response.status_code==200, response.text
    sanitized=response.json()['original_filename']
    assert len(sanitized)<=180
    assert sanitized.endswith(f'.{extension}')
    assert calls==[extension]


def test_strict_print_rejects_dropped_options(office_client):
    client, _, cups, _=office_client
    info=client.post('/files',files={'file':('test.pdf',make_pdf(),'application/pdf')}).json()
    request={'file_id':info['file_id'],'options':{'paper_size':'Nonsense','typo':True}}
    validation=client.post('/print/validate',json=request).json()
    assert not validation['valid']
    assert validation['unsupported_options']==['paper_size','typo']
    assert client.post('/print',json=request).status_code==422
    assert cups.submissions==[]
    assert client.post('/print',json={**request,'strict_options':False}).status_code==200


@pytest.mark.parametrize('status', [422,503,504,413])
def test_conversion_failure_cleans_upload(office_client,monkeypatch,status):
    client,storage,_,_=office_client
    def fail(*args):
        raise ConversionError('conversion failed',status)
    monkeypatch.setattr(OfficeConverter,'convert',fail)
    response=client.post('/files',files={'file':('file.xlsx',office_document('xlsx'))})
    assert response.status_code==status
    assert not list(storage.files_dir.iterdir())
    assert not list(storage.metadata_dir.iterdir())


def test_capabilities_tracks_runtime(office_client, monkeypatch):
    client,_,_,_=office_client
    monkeypatch.setattr(OfficeConverter,'format_availability',lambda _:{extension:False for extension in OFFICE_FORMATS})
    assert client.get('/capabilities').json()['office']['available'] is False
    monkeypatch.setattr(OfficeConverter,'format_availability',lambda _:{extension:extension in {'docx','odt'} for extension in OFFICE_FORMATS})
    response=client.get('/capabilities').json()['office']
    assert response['available'] is True
    assert {item['extension']:item['available'] for item in response['formats']}=={
        extension:extension in {'docx','odt'} for extension in OFFICE_FORMATS
    }


def test_format_availability_tracks_installed_components(tmp_path,monkeypatch):
    converter=OfficeConverter(tmp_path)
    monkeypatch.setattr(converter,'executable',lambda:'/usr/bin/libreoffice')
    monkeypatch.setattr('app.services.office_conversion.shutil.which',lambda name:'/usr/bin/dpkg-query' if name=='dpkg-query' else None)
    component_status={'writer':'installed','calc':'not-installed','impress':'not-installed'}
    def query(args,**kwargs):
        component=args[-1].removeprefix('libreoffice-')
        status=component_status[component]
        return subprocess.CompletedProcess(args,0 if status=='installed' else 1,status,'')
    monkeypatch.setattr('app.services.office_conversion.subprocess.run',query)

    assert converter.format_availability()=={
        'docx':True,'odt':True,'xlsx':False,'ods':False,'pptx':False,'odp':False,
    }


def test_converter_busy_and_missing_runtime(tmp_path,monkeypatch):
    converter=OfficeConverter(tmp_path)
    monkeypatch.setattr(converter,'executable',lambda:None)
    with pytest.raises(ConversionError) as error:
        converter.convert(tmp_path/'source','docx',tmp_path/'out.pdf')
    assert error.value.status_code==503
    monkeypatch.setattr(converter,'executable',lambda:'/unused')
    with (tmp_path/'.office.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with pytest.raises(ConversionError,match='busy'):
            converter.convert(tmp_path/'source','docx',tmp_path/'out.pdf')


def test_worker_blocks_network(tmp_path):
    from app.services import office_worker
    script="import socket; socket.socket(socket.AF_INET, socket.SOCK_STREAM)"
    result=subprocess.run([sys.executable,office_worker.__file__,'256','5','1048576',sys.executable,'-c',script],capture_output=True)
    assert result.returncode!=0
    assert b'Operation not permitted' in result.stderr


@pytest.mark.integration
@pytest.mark.parametrize('extension', OFFICE_FORMATS)
def test_real_libreoffice_conversion_and_preview(tmp_path, extension):
    if os.getenv('RUN_OFFICE_INTEGRATION')!='1':
        pytest.skip('Set RUN_OFFICE_INTEGRATION=1 inside Office-enabled image')
    source=tmp_path/f'source.{extension}'
    source.write_bytes(office_document(extension))
    inspect_office(source,source.name)
    destination=tmp_path/'converted.pdf'
    converter=OfficeConverter(tmp_path/'work')
    assert converter.executable(), 'LibreOffice and libseccomp must be installed'
    assert converter.convert(source,extension,destination)>=1
    pdf=PdfReader(destination)
    assert any('smoke test' in page.extract_text() for page in pdf.pages)
    from pdf2image import convert_from_path
    images=convert_from_path(str(destination),first_page=1,last_page=1,dpi=50,timeout=20)
    assert len(images)==1 and images[0].width>0
    images[0].close()
    assert not list((tmp_path/'work').glob('office-*'))


def test_timeout_kills_conversion_and_releases_lock(tmp_path,monkeypatch):
    executable=tmp_path/'fake-office'
    executable.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(30)\n')
    executable.chmod(0o700)
    source=tmp_path/'input'
    source.write_bytes(office_document('docx'))
    converter=OfficeConverter(tmp_path/'work')
    converter.timeout=1
    monkeypatch.setattr(converter,'executable',lambda:str(executable))
    for _ in range(2):
        with pytest.raises(ConversionError) as error:
            converter.convert(source,'docx',tmp_path/'out.pdf')
        assert error.value.status_code==504
        assert not list((tmp_path/'work').glob('office-*'))
    assert not (tmp_path/'out.pdf').exists()


def test_zero_exit_without_pdf_is_failure(tmp_path,monkeypatch):
    executable=tmp_path/'fake-office'
    executable.write_text('#!/bin/sh\nexit 0\n')
    executable.chmod(0o700)
    source=tmp_path/'input';source.write_bytes(office_document('docx'))
    converter=OfficeConverter(tmp_path/'work')
    monkeypatch.setattr(converter,'executable',lambda:str(executable))
    with pytest.raises(ConversionError,match='failed'):
        converter.convert(source,'docx',tmp_path/'out.pdf')


def test_output_file_limit_returns_413(tmp_path,monkeypatch):
    from app.services import office_conversion
    output_limit=1024*1024
    executable=tmp_path/'fake-office'
    executable.write_text(
        f'#!{sys.executable}\n'
        'from pathlib import Path\n'
        'import sys\n'
        'output=Path(sys.argv[sys.argv.index("--outdir")+1]) / "document.pdf"\n'
        f'output.write_bytes(b"x" * {output_limit + 1})\n'
    )
    executable.chmod(0o700)
    source=tmp_path/'input';source.write_bytes(office_document('docx'))
    converter=OfficeConverter(tmp_path/'work')
    monkeypatch.setattr(converter,'executable',lambda:str(executable))
    monkeypatch.setattr(office_conversion,'OUTPUT_LIMIT_BYTES',output_limit)

    with pytest.raises(ConversionError) as error:
        converter.convert(source,'docx',tmp_path/'out.pdf')
    assert error.value.status_code==413
    assert 'size limit' in error.value.message
    assert not (tmp_path/'out.pdf').exists()
    assert not list((tmp_path/'work').glob('office-*'))


def test_converted_page_limit(tmp_path,monkeypatch):
    source=tmp_path/'input';source.write_bytes(office_document('docx'))
    template=tmp_path/'template.pdf';template.write_bytes(make_pdf(2))
    executable=tmp_path/'fake-office'
    executable.write_text(f'#!{sys.executable}\nimport sys,shutil\nfrom pathlib import Path\nshutil.copyfile({str(template)!r},Path(sys.argv[sys.argv.index("--outdir")+1])/"document.pdf")\n')
    executable.chmod(0o700)
    converter=OfficeConverter(tmp_path/'work');converter.max_pages=1
    monkeypatch.setattr(converter,'executable',lambda:str(executable))
    with pytest.raises(ConversionError) as error:
        converter.convert(source,'docx',tmp_path/'out.pdf')
    assert error.value.status_code==413
    assert not (tmp_path/'out.pdf').exists()


def test_archive_limit_and_invalid_format(tmp_path,monkeypatch):
    from app.services import office_formats
    source=tmp_path/'input';source.write_bytes(office_document('xlsx'))
    monkeypatch.setattr(office_formats,'MAX_EXPANDED_BYTES',100)
    with pytest.raises(OfficeFormatError,match='limit'):
        inspect_office(source,'file.xlsx')
    source.write_bytes(b'plain text')
    with pytest.raises(OfficeFormatError,match='unreadable'):
        inspect_office(source,'file.docx')
    with pytest.raises(OfficeFormatError,match='Legacy'):
        inspect_office(source,'file.xls')


def test_cleanup_removes_converted_pdf_but_respects_lease(office_client):
    client,storage,_,_=office_client
    info=client.post('/files',files={'file':('file.docx',office_document('docx'))}).json()
    file_id=info['file_id']
    os.utime(storage.metadata_path(file_id),(1,1))
    with storage.lease(file_id):
        assert storage.prune_expired(2,set())==0
        assert storage.converted_path(file_id).exists()
    assert storage.prune_expired(2,set())==1
    assert not storage.converted_path(file_id).exists()
    assert client.get(info['pdf_url']).status_code==404


def test_corrupt_image_and_empty_pdf_rejected(office_client):
    client,_,_,_=office_client
    assert client.post('/files',files={'file':('bad.png',b'\x89PNG\r\n\x1a\ninvalid')}).status_code==400
    assert client.post('/files',files={'file':('empty.pdf',make_pdf(0))}).status_code==400


def test_print_requires_capabilities_in_strict_mode(office_client,monkeypatch):
    client,_,cups,_=office_client
    info=client.post('/files',files={'file':('file.pdf',make_pdf())}).json()
    monkeypatch.setattr(cups,'get_option_capabilities',lambda:{})
    assert client.post('/print/validate',json={'file_id':info['file_id']}).status_code==503
    assert client.post('/print',json={'file_id':info['file_id']}).status_code==503
    assert cups.submissions==[]


def test_openapi_documents_runtime_paths_and_required_fields(office_client):
    client,_,_,_=office_client
    spec=client.get('/openapi.json').json()
    for path in ['/capabilities','/files/{file_id}','/files/{file_id}/pdf','/print/validate']:
        assert path in spec['paths']
    assert '504' in spec['paths']['/files/{file_id}/preview/{page}']['get']['responses']
    assert 'converted output' in spec['paths']['/files']['post']['responses']['413']['description']
    schemas=spec['components']['schemas']
    assert schemas['PrintRequest']['properties']['strict_options']['default'] is True
    assert set(schemas['CapabilitiesResponse']['required'])<=set(client.get('/capabilities').json())
    upload=client.post('/files',files={'file':('file.docx',office_document('docx'))}).json()
    assert set(schemas['FileUploadResponse']['required'])<=set(upload)
    validation=client.post('/print/validate',json={'file_id':upload['file_id']}).json()
    assert set(schemas['PrintValidationResponse']['required'])<=set(validation)


@pytest.mark.integration
@pytest.mark.parametrize('extension',['docx','xlsx'])
def test_real_office_upload_preview_and_print_preflight(tmp_path,extension):
    if os.getenv('RUN_OFFICE_INTEGRATION')!='1':
        pytest.skip('Set RUN_OFFICE_INTEGRATION=1 inside Office-enabled image')
    storage=TempFileStorage(tmp_path/'storage')
    cups=FakeCupsClient()
    app.dependency_overrides[get_file_storage]=lambda:storage
    app.dependency_overrides[get_cups_client]=lambda:cups
    try:
        with TestClient(app) as client:
            signup_user(client)
            upload=client.post('/files',files={'file':(f'file.{extension}',office_document(extension))})
            assert upload.status_code==200,upload.text
            info=upload.json()
            pdf=client.get(info['pdf_url']).content
            assert hashlib.sha256(pdf).hexdigest()==info['printable_sha256']
            assert 'smoke test' in PdfReader(BytesIO(pdf)).pages[0].extract_text()
            preview=client.get(f"/files/{info['file_id']}/preview/1")
            assert preview.status_code==200 and preview.content.startswith(b'\x89PNG')
            request={'file_id':info['file_id'],'options':{'pages':'1','paper_size':'A4','color_mode':'monochrome','duplex':'none'}}
            validation=client.post('/print/validate',json=request)
            assert validation.status_code==200 and validation.json()['valid']
            printed=client.post('/print',json=request)
            assert printed.status_code==200,printed.text
            assert printed.json()['applied_options']==validation.json()['applied_options']
            assert client.get(info['pdf_url']).content==pdf
            assert len(cups.submissions)==1
    finally:
        app.dependency_overrides.clear()


@pytest.mark.integration
def test_real_spreadsheet_preserves_print_area_and_hidden_sheet(tmp_path):
    if os.getenv('RUN_OFFICE_INTEGRATION')!='1':
        pytest.skip('Set RUN_OFFICE_INTEGRATION=1 inside Office-enabled image')
    with ZipFile(BytesIO(office_document('xlsx'))) as source:
        entries={name:source.read(name) for name in source.namelist()}
    entries['xl/workbook.xml']=entries['xl/workbook.xml'].replace(
        b'</sheets>',b'<sheet name="Secret" sheetId="2" state="hidden" r:id="rId2"/></sheets><definedNames><definedName name="_xlnm.Print_Area" localSheetId="0">Visible!$A$1:$A$2</definedName></definedNames>')
    entries['xl/_rels/workbook.xml.rels']=entries['xl/_rels/workbook.xml.rels'].replace(
        b'</Relationships>',b'<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
    entries['[Content_Types].xml']=entries['[Content_Types].xml'].replace(
        b'</Types>',b'<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
    entries['xl/worksheets/sheet2.xml']=entries['xl/worksheets/sheet1.xml'].replace(b'XLSX smoke test',b'Hidden secret')
    entries['xl/worksheets/sheet1.xml']=entries['xl/worksheets/sheet1.xml'].replace(
        b'</sheetData>',b'<row r="20"><c r="A20" t="inlineStr"><is><t>Outside print area</t></is></c></row></sheetData>')
    source=tmp_path/'layout.xlsx'
    with ZipFile(source,'w') as archive:
        for name,data in entries.items():
            archive.writestr(name,data)
    assert inspect_office(source,source.name)=='xlsx'
    output=tmp_path/'out.pdf'
    OfficeConverter(tmp_path/'work').convert(source,'xlsx',output)
    text='\n'.join(page.extract_text() for page in PdfReader(output).pages)
    assert 'smoke test' in text
    assert 'Hidden secret' not in text
    assert 'Outside print area' not in text


@pytest.mark.parametrize('content',[
    b'<w:x xmlns:w="urn:word"><w:instrText>DD</w:instrText><w:instrText>EAUTO command</w:instrText></w:x>',
    b'<w:x xmlns:w="urn:word"><w:fldSimple w:instr="INCLUDETEXT file:///etc/passwd"/></w:x>',
    b'<x><dde-source/></x>',
])
def test_active_fields_rejected(tmp_path,content):
    buffer=BytesIO(office_document('docx'))
    with ZipFile(buffer,'a') as archive:
        archive.writestr('word/fields.xml',content)
    path=tmp_path/'file.docx';path.write_bytes(buffer.getvalue())
    with pytest.raises(OfficeFormatError):
        inspect_office(path,path.name)


def test_preview_timeout_is_explicit(office_client,monkeypatch):
    import pdf2image
    from pdf2image.exceptions import PDFPopplerTimeoutError
    client,_,_,_=office_client
    info=client.post('/files',files={'file':('file.pdf',make_pdf())}).json()
    def fail(*args,**kwargs):
        raise PDFPopplerTimeoutError('timeout')
    monkeypatch.setattr(pdf2image,'convert_from_path',fail)
    response=client.get(f"/files/{info['file_id']}/preview/1")
    assert response.status_code==504


def test_crash_workspaces_are_cleaned_only_when_not_converting(tmp_path):
    storage=TempFileStorage(tmp_path)
    work=tmp_path/'conversion-work';work.mkdir()
    stale=work/'office-stale';stale.mkdir();(stale/'input.docx').write_bytes(b'old')
    os.utime(stale,(1,1))
    with (work/'.office.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        storage.prune_expired(2,set())
        assert stale.exists()
    storage.prune_expired(2,set())
    assert not stale.exists()
