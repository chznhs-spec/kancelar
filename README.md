# Poscilogramář

Ruční editor digitálních logických průběhů ve stylu Windows 98. Aplikace umí současně až 10 kanálů, ukládá pracovní plochy do kompatibilního kontejneru `.vlcr` a exportuje výsledný diagram do PNG, PDF a SVG.

## Spuštění

Požadavky: Python 3.9+ a PyQt5.

```bash
python -m pip install -r requirements.txt
python poscilogramar.py
```

Soubor `.vlcr` lze otevřít přímo:

```bash
python poscilogramar.py "Cesta/k/souboru.vlcr"
```

## Rozcestník VLČR Kancelář

Součástí balíčku je upravený `main.py` podle dodaného rozcestníku. Na domovské stránce přibyla dlaždice **Poscilogramář** a v levé navigaci samostatná položka. Tlačítko aplikaci spustí jako samostatný proces.

Pro běžný vývoj ponechte `main.py`, `poscilogramar.py`, `vlcr_format.py`, `pruzkumnik.py` a složku `loga` ve společném adresáři. Rozcestník spouštějte příkazem `python main.py`.

## Průzkumník souborů VLČR

V balíčku je také upravená verze `pruzkumnik.py`. Rozpoznává aplikaci `poscilogramar`, zobrazuje ji jako **Poscilogramář**, umí vytvořit nový pracovní soubor z nabídky **Nový** a při otevření `.vlcr` spustí `poscilogramar.py` s cestou k dokumentu.

## Ovládání

- **Malovat průběh:** kliknutí na svislou mřížku přepne logickou úroveň od daného okamžiku doprava. Výchozí úroveň je 0.
- **Posouvat:** tažením myši posunete časovou osu. Funguje také **Shift + kolečko myši**.
- **Označit kanál:** kliknutím vyberete řádek kanálu.
- Dvojklik na název kanálu vyvolá jeho přejmenování.
- `Ctrl` + `+` / `Ctrl` + `-` nebo `Ctrl` + kolečko myši mění časovou jednotku jednoho dílku. Výchozí hodnota je **1 µs/dílek**.
- Změna měřítka nemění uložené časy hran. Průběhy se při přiblížení a oddálení vykreslí ve správné absolutní časové délce; po změně měřítka tak lze do stejného diagramu kreslit například 1µs i 1ms impulsy.
- Export zahrnuje celý diagram (nejen právě viditelný výřez).

## Formát `.vlcr`

Aplikace používá osmibajtový identifikátor `VLCRPOSC`, verzi kontejneru, délku JSON metadat, JSON metadata a komprimovaný JSON payload (`zlib`). Metadata obsahují `app: "poscilogramar"`, takže je Průzkumník VLČR umí zařadit ke správné aplikaci.

Nová verze ukládá hrany jako absolutní časy v nanosekundách. Starší soubory Poscilogramáře s hranami uloženými v indexech dílků se při otevření automaticky převedou na absolutní časy.
