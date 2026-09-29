from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED

from app.services.office_formats import OFFICE_FORMATS


def office_document(extension: str) -> bytes:
    buffer = BytesIO()
    mime, main, _ = OFFICE_FORMATS[extension]
    with ZipFile(buffer, 'w', ZIP_DEFLATED) as z:
        if extension.startswith('od'):
            z.writestr('mimetype', mime)
            body = {
                'odt': '<office:text><text:p>Office document smoke test</text:p></office:text>',
                'ods': '<office:spreadsheet><table:table table:name="Visible"><table:table-row><table:table-cell office:value-type="string"><text:p>Spreadsheet smoke test</text:p></table:table-cell></table:table-row></table:table></office:spreadsheet>',
                'odp': '<office:presentation><draw:page draw:name="Slide 1"><draw:frame svg:x="1cm" svg:y="1cm" svg:width="15cm" svg:height="5cm"><draw:text-box><text:p>Presentation smoke test</text:p></draw:text-box></draw:frame></draw:page></office:presentation>',
            }[extension]
            z.writestr('content.xml', '<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" office:version="1.2"><office:body>'+body+'</office:body></office:document-content>')
            z.writestr('META-INF/manifest.xml', '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0"><manifest:file-entry manifest:full-path="/" manifest:media-type="'+mime+'"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')
        else:
            relns='http://schemas.openxmlformats.org/package/2006/relationships'
            officens='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
            content_type={
                'docx':'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml',
                'xlsx':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml',
                'pptx':'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml',
            }[extension]
            extra=''
            if extension=='xlsx':
                extra='<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            if extension=='pptx':
                extra='<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
            z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/'+main+'" ContentType="'+content_type+'"/>'+extra+'</Types>')
            z.writestr('_rels/.rels',f'<Relationships xmlns="{relns}"><Relationship Id="rId1" Type="{officens}/officeDocument" Target="{main}"/></Relationships>')
            if extension=='docx':
                z.writestr(main,'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>DOCX smoke test</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="11906" w:h="16838"/></w:sectPr></w:body></w:document>')
            elif extension=='xlsx':
                z.writestr(main,f'<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="{officens}"><sheets><sheet name="Visible" sheetId="1" r:id="rId1"/></sheets></workbook>')
                z.writestr('xl/_rels/workbook.xml.rels',f'<Relationships xmlns="{relns}"><Relationship Id="rId1" Type="{officens}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
                z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>XLSX smoke test</t></is></c></row></sheetData><pageSetup paperSize="9" orientation="portrait"/></worksheet>')
            else:
                z.writestr(main,f'<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="{officens}"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>')
                z.writestr('ppt/_rels/presentation.xml.rels',f'<Relationships xmlns="{relns}"><Relationship Id="rId1" Type="{officens}/slide" Target="slides/slide1.xml"/></Relationships>')
                z.writestr('ppt/slides/slide1.xml','<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm><a:off x="914400" y="914400"/><a:ext cx="7000000" cy="2000000"/></a:xfrm></p:spPr><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>PPTX smoke test</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>')
    return buffer.getvalue()
