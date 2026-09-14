# Arrangementsvarsler for Orkland-regionen

Dette prosjektet lager én RSS-feed med arrangementer fra:

- Visit Orkland
- MittSkaun
- iHeim
- Kulturboksen i Rindal

GitHub Actions oppdaterer feeden på følgende tidspunkt i tidssonen
`Europe/Oslo`:

- Mandag–fredag kl. 15.00: arrangementer som pågår kl. 15.30, eller starter
  senere samme dag.
- Fredag kl. 08.30: arrangementer som pågår eller starter påfølgende lørdag
  og søndag.
- Det kjøres ikke dagsvarsel lørdag eller søndag.

Hver oversikt blir ett RSS-innlegg. Hvis ingen arrangementer finnes, blir det
ikke laget noe innlegg.

## Kom i gang på GitHub

1. Opprett et nytt GitHub-repository.
2. Pakk ut innholdet i denne pakken i repository-roten og push til `main`.
3. Åpne **Settings → Pages** i GitHub.
4. Velg **GitHub Actions** under **Build and deployment → Source**.
5. Åpne fanen **Actions**, velg arbeidsflyten **Oppdater arrangementsfeed** og
   kjør den manuelt med `daily` eller `weekend` for å teste.
6. Abonner på feeden:

   `https://DITT-GITHUB-NAVN.github.io/REPOSITORY-NAVN/feed.xml`

Det kan ta et par minutter før GitHub Pages-adressen virker første gang.
RSS-leseren bestemmer selv hvor ofte feeden kontrolleres, så telefonvarselet kan
komme senere enn tidspunktet GitHub oppdaterer feeden.

## Hva prosjektet gjør

- henter bare perioden som er nødvendig for det aktuelle varselet
- bruker åpne maskinlesbare endepunkter i stedet for nettleserautomatisering
- tar hensyn til norsk sommer- og vintertid
- tar med arrangementer som allerede er i gang ved starttidspunktet
- håndterer gjentakelser som egne forekomster
- fjerner identiske treff fra samme tidspunkt og sted
- beholder de 60 siste RSS-innleggene
- publiserer ikke en ufullstendig oversikt dersom en kilde feiler

## Lokal testing

Python 3.11 eller nyere er tilstrekkelig; prosjektet har ingen eksterne
avhengigheter.

```bash
python -m unittest discover -s tests -v
python src/event_feed.py --mode daily --output docs/feed.xml --history docs/history.json
python src/event_feed.py --mode weekend --output docs/feed.xml --history docs/history.json
python src/event_feed.py --check-sources
```

Ved lokal prøvekjøring blir dagens dato i `Europe/Oslo` brukt. En eksisterende
oversikt med samme type og dato blir ikke lagt til på nytt.

## Vedlikehold

Orkland-kilden har et uttrykkelig åpent GraphQL-grensesnitt. Endepunktene hos
Skaun, Heim og Rindal er offentlige endepunkter som nettsidene selv bruker, men
de er ikke dokumenterte utvikler-API-er. Testene oppdager endringer i vårt eget
format, mens `--check-sources` kan brukes for å kontrollere live-kildene.

