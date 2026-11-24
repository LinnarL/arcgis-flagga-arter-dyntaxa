# Flagga arter från Dyntaxa

ArcGIS Pro-verktyg som slår upp artnamnen i ett fält i Dyntaxa och sätter upp till tre valfria fält till Ja eller Nej: rödlistad, skyddad (fridlyst) och invasiv. Namn som inte stavas exakt matchas mot närmaste träff. Namn som inte kan matchas varnas om och lämnas oförändrade.

## Krav

- ArcGIS Pro 3.x (utvecklat och testat på 3.6). Inga paket utöver dem som följer med Pro.
- Prenumerationsnyckel från [api-portal.artdatabanken.se](https://api-portal.artdatabanken.se/):
  - Dyntaxa Taxon Service räcker för rödlistad.
  - Species Observation System (SOS) krävs för skyddad och invasiv. SOS-nyckeln fungerar också mot Dyntaxa, så en nyckel med båda prenumerationerna räcker.
- Internetåtkomst till `api.artdatabanken.se`.

## Lägga till i ArcGIS Pro

1. Öppna Catalog-fönstret, högerklicka på Toolboxes och välj Add Toolbox.
2. Välj `FlaggaArterDyntaxa.pyt`.
3. Öppna verktyget Flagga arter (rödlistad, skyddad, invasiv).

## Parametrar

| Parameter | Beskrivning |
|---|---|
| Indatatabell | Lager eller tabell. Fälten ändras på plats, som i Beräkna fält. Urval och definitionsfråga gäller: bara de raderna läses och ändras. |
| Artnamnsfält | Textfält med svenska eller vetenskapliga namn eller synonymer, med eller utan auktor. |
| Fält för rödlistad, skyddad, invasiv | Valfria. Textfält får Ja/Nej, heltalsfält 1/0. Har fältet en kodad domän används dess ja/nej-värden. Minst ett fält måste väljas. |
| Rödlistekategorier | Standard RE, CR, EN, VU, NT. DD kan läggas till. |
| Artlistor för invasiv | Standard EU-förordning 1143/2014 och Nationell förteckning över invasiva främmande arter. Alla främmande arter i Sverige kan väljas. |
| Minsta likhet (%) | Standard 85. Gräns för närmaste träff. 100 kräver exakt namn. |
| Dyntaxa-nyckel, SOS-nyckel | Lämnas tomma om nyckeln är sparad eller finns i miljövariabeln `DYNTAXA_KEY` respektive `ARTDATABANKEN_API_KEY`. |
| Spara nycklarna | Sparar i Windows Autentiseringshanteraren, samma post som verktyget Rapportera till Artportalen. |

## Så sätts fälten

- Rödlistad: taxonets egen kategori i Dyntaxa ingår i de valda kategorierna.
- Skyddad: taxonet finns i Artdatabankens artlista Fridlysta arter (SOS lista 1).
- Invasiv: taxonet finns i någon av de valda artlistorna.
- Skyddad och invasiv ärvs nedåt: en underart av en fridlyst art räknas som skyddad. Rödlistad ärvs inte, eftersom kategorin är satt per taxon.

## Att tänka på

- Nej betyder att taxonet hittades och inte ingår. Rader vars namn inte kan matchas, och rader utan namn, lämnas oförändrade och varnas om, så ett gammalt värde i fältet blir kvar.
- Närmaste träff: verktyget söker på delar av namnet och rankar med difflib. Träffen måste nå minsta likhet och får inte ligga lika nära ett annat taxon (en art och dess underart räknas som samma träff, arten väljs). Varje närmaste träff skrivs i loggen så att den kan kontrolleras. Namnen i fältet ändras aldrig.
- Namn som slutar på sp. matchas mot släktet, och namn bestämda högre än art ger en varning, eftersom Ja/Nej då gäller släktet och inte dess arter.
- Spara eller stoppa pågående redigering innan verktyget körs. Verktyget skriver direkt i datat.
- Uppslagningen tar ungefär 1 sekund per unikt namn (3 till 4 sekunder för närmaste träff), fördelat på fyra anrop samtidigt. Samma namn slås bara upp en gång.
- Rödlistekategori och artlistor hämtas vid varje körning, så resultatet följer Artdatabankens aktuella listor.
