"""Maak HTML-ingangen naast de ongewijzigde XHTML-bestanden van de EPUB.

Gebruik: python scripts/build-web-html-compat.py
Vereist Python 3 en lxml. Geen TeX, vertaling of netwerkverkeer.
"""
from pathlib import Path
from lxml import etree, html
import hashlib, json

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'docs/readers/160-r5'
rows = []
def digest(data):
    return hashlib.sha256(data).hexdigest()
def local(tag):
    return tag.rsplit('}', 1)[-1] if isinstance(tag, str) else ''
def elements(node):
    return [x for x in node.iter() if isinstance(x.tag, str)]
for register in ('nl-standard', 'nl-gewoon'):
    for rel in ('OEBPS/generated/book.xhtml', 'OEBPS/nav.xhtml'):
        src = WEB / register / rel
        raw = src.read_bytes()
        tree = etree.fromstring(raw, etree.XMLParser(resolve_entities=False, no_network=True))
        before_text = ''.join(tree.itertext())
        before_math = sum(local(x.tag) == 'math' for x in elements(tree))
        before_ids = [x.get('id') for x in elements(tree) if x.get('id')]
        for node in elements(tree):
            if node.tag.startswith('{http://www.w3.org/1999/xhtml}'):
                node.tag = local(node.tag)
            href = node.get('href')
            if href:
                node.set('href', href.replace('book.xhtml', 'book.html').replace('nav.xhtml', 'nav.html'))
        out = etree.tostring(tree, method='html', encoding='utf-8', doctype='<!DOCTYPE html>')
        parsed = html.fromstring(out)
        assert ''.join(parsed.itertext()) == before_text, 'Tekstwijziging'
        assert [x.get('id') for x in elements(parsed) if x.get('id')] == before_ids, 'Ankerwijziging'
        assert sum(local(x.tag) == 'math' for x in elements(parsed)) == before_math, 'Formuleverlies'
        assert len(elements(parsed)) == len(elements(tree)), 'Structuurwijziging'
        target = src.with_suffix('.html')
        target.write_bytes(out)
        rows.append({'path': str(target.relative_to(ROOT)).replace('\\', '/'), 'bytes':len(out), 'sha256':digest(out), 'source_path':str(src.relative_to(ROOT)).replace('\\', '/'), 'source_sha256':digest(raw), 'text_identical':True, 'anchors_identical':True, 'math_elements':before_math, 'element_count':len(elements(parsed))})
report = {'schema':'openlogic-html-compat/1', 'status':'PASS', 'scope':'160/722 per register; geen vertaalwijzigingen', 'files':rows, 'epub_and_source_archives_changed':False}
(ROOT / 'provenance/DUTCH_HTML_COMPAT_20260927.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')
print(json.dumps({'status':'PASS','html_files':len(rows),'math_elements':[r['math_elements'] for r in rows]}))
