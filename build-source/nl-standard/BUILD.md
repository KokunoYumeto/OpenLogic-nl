# Bronpakket — Wetenschappelijk Nederlands

Dit is een gedeeltelijke bronuitgave: 160 van 722 broneenheden in twee Nederlandse registers. De volledige oorspronkelijke wiskundige inhoud van deze eenheden is behouden; de laatste hoofdstukgrens kan midden in een hoofdstuk vallen. Er is nog geen gepubliceerde PDF of EPUB. De aangeboden samengestelde LaTeX is inhoudelijk samengesteld en bytegewijs gecontroleerd, maar in deze uitgave niet gecompileerd. Een eerdere bouwpoging kon de gedeelde TeX-vergrendeling niet verkrijgen en startte geen TeX-proces. De termen “geaccepteerd” en “gecontroleerd” verwijzen naar de vastgelegde AI-productiecontroles, niet naar onafhankelijk deskundigenonderzoek.

## Inhoud en gebruik

Het bestand `OpenLogic-NL-Standard-Partial-OLP-0001--OLP-0160.tex` bevat alle 160 opgenomen tekstbestanden. De oorspronkelijke modulaire vertalingen, stijlen, bibliografie, figuren en Nederlandse labels staan in dit pakket. De volledige `alignment/UNITS.jsonl` bewaart alleen de broninventaris en grensinformatie; latere vertaalbestanden zijn niet opgenomen.

Vereisten: Python 3.11 of nieuwer met lxml en pypdf, een volledige TeX-installatie, latexmk, pdfLaTeX, BibTeX, make4ht/TeX4ht, Java en EPUBCheck 5.3.0. Het bestand `PDF-REBUILD.json` beschrijft de bouwopdrachten. De meegeleverde bouwer gebruikt op Windows de globale TeX-vergrendeling; start geen parallelle TeX-processen. Het bouwresultaat is nog niet voor deze bronuitgave vastgesteld.

Voer vanuit de uitgepakte map uit:

~~~text
python scripts/build_partial_epub3.py --repo-root . --output-dir rebuilt --register nl-standard --modified 2026-09-26
~~~

Dezelfde bronuitgave staat in [het Nederlandse project](https://github.com/KokunoYumeto/OpenLogic-nl).

## AI-verantwoording

Vertaling en eerdere controles: OpenAI Codex; de huidige eigenaar gebruikt GPT-5.6 Sol, Ultra effort. De exacte historische model- en inspanningsinstelling van ieder tekstgedeelte zijn nog niet afzonderlijk bewezen; de huidige instelling wordt daarom niet aan alle eerdere vertaalbytes toegeschreven. Bronselectie, verpakking en publicatiecontrole voor deze uitgave: OpenAI Codex — GPT-6 Astra, Ultra effort. Geen menselijke redactie of beoordeling wordt geclaimd.
