# RSS Text - Terminal RSS Reader

RSS hírolvasó terminálalkalmazás Textual frameworkkel.

## Funkciók

- RSS feedek követése több forrásból
- Cikkek listázása és előnézete
- Teljes cikkek letöltése newspaper4k-val
- Billentyűalapú navigáció
- Képek megjelenítése természetben

## Telepítés

A projekt `uv`-val van menedzselve:

```bash
uv sync
```

## Futtatás

```bash
uv run python main.py
```

## Billentyűparancsok

- `Tab`: Váltás a lista és a részletek között
- `j` / `↓`: Le a listán
- `k` / `↑`: Fel a listán
- `Enter`: Teljes cikk megnyitása új ablakban
- `Esc`: Ablak bezárása (cikk nézetben)

## RSS Források

Jelenleg követett források:
- telex.hu
- 444.hu
- hvg.hu
- magyarnarancs.hu
- 24.hu
- hang.hu

Hozzáférés: `main.py` SOURCES dictionary.

