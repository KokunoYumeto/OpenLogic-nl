# Deze begrensde reader opnieuw bouwen

De vertaalde bron in deze map bevat alleen de aaneengesloten geaccepteerde reeks OLP-0001 tot en met OLP-0192 voor `nl-standard`. Er staat bewust geen later vertaalbestand in. Het volledige gezaghebbende `UNITS.jsonl` blijft aanwezig als bewijs van de grens; `ACCEPTED_PREFIX.jsonl` bevat de gekozen rijen.

Vertaling en eerdere controles: OpenAI Codex; de huidige eigenaar gebruikt GPT-5.6 Sol, Ultra effort. De exacte historische model- en inspanningsinstelling van ieder tekstgedeelte zijn nog niet afzonderlijk bewezen; de huidige instelling wordt daarom niet aan alle eerdere vertaalbytes toegeschreven. Samenstelling van deze reader, directe EPUB-conversie en deterministische bouwcontroles: OpenAI Codex — GPT-5.6 Sol, Ultra effort. Geen menselijke redactie of beoordeling wordt geclaimd.

Gebruik voor de rechtstreeks downloadbare samengestelde LaTeX, de volledige bron-ZIP en de EPUB de bouwer zonder TeX. Vereisten: Python 3.11+ met lxml, Pandoc, Java en de bytevastgelegde EPUBCheck-runtime 5.3.0 (`epubcheck.jar` en `lib/*.jar`; boom-SHA-256 `f5e057f7b81e1a527c53f8fabe63c17681ddc63919343c05eb945a6a1768face`). Geef het JAR-bestand door met `--epubcheck-jar` of stel `OPENLOGIC_EPUBCHECK_JAR` in. Pandoc maakt native MathML en wordt tweemaal uitgevoerd; de volledige gegenereerde bestandsbomen moeten byte voor byte gelijk zijn. Pandoc en EPUBCheck starten geen TeX en gebruiken de TeX-mutex niet. Voer vanuit de hoofdmap van deze uitgepakte bronboom uit:

```text
python scripts/build_partial_source_epub3.py --repo-root . --output-dir rebuilt --register nl-standard --modified 2026-09-27
```

Een optionele PDF-herbouw vereist daarnaast Python met pypdf, latexmk, pdfTeX/pdflatex, BibTeX en de LaTeX-pakketten die de meegeleverde Open Logic-stijlen gebruiken. Onder Windows houdt die PDF-bouwer `Global\InterlanguageTeXSlotV1` voortdurend vast rond beide PDF-procesbomen en rond de afzonderlijke oude make4ht-procesboom. Iedere starter wordt eerst gepauzeerd aangemaakt, aan de Windows-job met kill-on-close gekoppeld en pas daarna hervat. `PDF-REBUILD.json` bevat de exacte opdrachten, vaste omgeving, meegeleverde afhankelijkheden en de regel voor bytegelijke herhaling. De bouwer controleert PDF-kop, trailer, leesbaarheid en paginatal; het ontvangstbewijs markeert rasterweergave en visuele controle uitdrukkelijk als nog niet uitgevoerd. Optionele opdracht voor PDF en de oude EPUB-route:

```text
python scripts/build_partial_epub3.py --repo-root . --output-dir rebuilt --register nl-standard --modified 2026-09-27
```
