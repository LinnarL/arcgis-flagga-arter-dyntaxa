# -*- coding: utf-8 -*-
"""
Flagga arter från Dyntaxa
=========================

Läser artnamn ur ett fält i ett lager eller en tabell, slår upp varje namn i Dyntaxa
och sätter upp till tre valfria fält till Ja/Nej: rödlistad, skyddad och invasiv. Som
Beräkna fält ändras indata på plats; fälten väljs av användaren och tabellens urval och
definitionsfråga gäller (bara de raderna läses och ändras).

Datakällor
----------
Dyntaxa Taxon Service : https://api.artdatabanken.se/taxonservice/v1
                        GET /taxa/search?searchString=&searchFields=Both&pageSize=&page=
                        GET /taxa/{id}/parentids
                        Taxonet bär redlistCategory, till exempel "Sårbar (VU)". Det finns
                        ingen uppslagning på id, bara sökning och föräldrar/barn.
Species Observation System (SOS), artlistor:
                        https://api.artdatabanken.se/species-observation-system/v1
                        GET /TaxonLists/{id}/Taxa -> [{id, scientificName, swedishName, ...}]
                        1 Fridlysta arter, 4 Främmande arter i Sverige, 5 EU-förordning
                        1143/2014, 42 Nationell förteckning över invasiva främmande arter
                        (id kontrollerade 2026-11-24).
Nycklar               : https://api-portal.artdatabanken.se/ (Ocp-Apim-Subscription-Key).
                        En Dyntaxa-nyckel räcker för namn och rödlistning. SOS-nyckeln krävs
                        för skyddad och invasiv, och fungerar också mot Dyntaxa (kontrollerat);
                        Dyntaxa-nyckeln fungerar inte mot SOS (HTTP 401).

Regler
------
- Namnet jämförs utan hänsyn till versaler med alla namn (svenska, vetenskapliga,
  synonymer) på accepterade taxa. Auktor, "cf." och avslutande "sp." tas bort först.
- Finns inget exakt namn används närmaste träff: söker på delar av namnet och rankar med
  difflib. Träffen måste nå Minsta likhet och får inte ligga nära ett annat taxon.
- Rödlistad: taxonets egen kategori i Dyntaxa ingår i de valda kategorierna. Skyddad och
  invasiv: taxonet eller något taxon över det (art, släkte, familj) finns i artlistan, så
  en underart av en fridlyst art räknas som skyddad.
- Namn som inte kan matchas (eller är tomma) lämnas oförändrade och varnas om. Nej betyder
  alltså att taxonet hittades och inte ingår i listan, aldrig att uppgiften saknas.
- Fältvärden: textfält får Ja/Nej, heltalsfält 1/0. Har fältet en kodad domän används den
  kod vars kod eller beskrivning är ja/nej (1/0, yes/no, true/false).

Verktygstipsen skrivs till <verktygslåda>.<verktyg>.pyt.xml från TOOLTIPS när
verktygslådan laddas.
"""
import concurrent.futures
import difflib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

import arcpy

# =============================================================================
# Konstanter
# =============================================================================

DYNTAXA_API = "https://api.artdatabanken.se/taxonservice/v1"
SOS_API = "https://api.artdatabanken.se/species-observation-system/v1"
ENV_DYNTAXA = "DYNTAXA_KEY"
ENV_SOS = "ARTDATABANKEN_API_KEY"
# Samma post som Rapportera till Artportalen, så en sparad nyckel gäller båda verktygen.
CREDENTIAL_SERVICE = "Artdatabanken API (ArcGIS-verktyg)"
CREDENTIAL_DYNTAXA = "dyntaxa"
CREDENTIAL_SOS = "sos"
USER_AGENT = "ArcGIS-FlaggaArterDyntaxa"
HTTP_TIMEOUT = 60
HTTP_RETRIES = 6

PAGE_SIZE = 100
MAX_PAGES = 3
WORKERS = 4
MAX_LISTED = 30                # högst så många namn per varning i loggen
AMBIGUITY_MARGIN = 0.02        # närmaste träff får inte ha ett annat taxon så här nära

DEFAULT_MIN_SIMILARITY = 85

LIST_PROTECTED = 1             # Fridlysta arter
RED_CATEGORIES = {             # visningsnamn -> förkortning i Dyntaxas redlistCategory
    "Nationellt utdöd (RE)": "RE",
    "Akut hotad (CR)": "CR",
    "Starkt hotad (EN)": "EN",
    "Sårbar (VU)": "VU",
    "Nära hotad (NT)": "NT",
    "Kunskapsbrist (DD)": "DD",
}
DEFAULT_RED = ["Nationellt utdöd (RE)", "Akut hotad (CR)", "Starkt hotad (EN)",
               "Sårbar (VU)", "Nära hotad (NT)"]
INVASIVE_LISTS = {             # visningsnamn -> artlistans id i SOS
    "EU-förordning 1143/2014 (Unionsförteckningen)": 5,
    "Nationell förteckning över invasiva främmande arter": 42,
    "Alla främmande arter i Sverige": 4,
}
DEFAULT_INVASIVE = ["EU-förordning 1143/2014 (Unionsförteckningen)",
                    "Nationell förteckning över invasiva främmande arter"]

YES_WORDS = {"ja", "yes", "1", "true", "j", "y"}
NO_WORDS = {"nej", "no", "0", "false", "n"}

TOOL_SUMMARY = (
    "Slår upp artnamn i Dyntaxa och sätter valda fält till Ja eller Nej för rödlistad, "
    "skyddad (fridlyst) och invasiv art. Namn som inte stavas exakt matchas mot närmaste "
    "träff. Namn som inte kan matchas varnas om och lämnas oförändrade."
)

TOOLTIPS = {
    "in_table": (
        "Lager eller tabell med artnamnen. Fälten ändras på plats, som i Beräkna fält. "
        "Finns ett urval eller en definitionsfråga på lagret läses och ändras bara de raderna. "
        "Spara eller stoppa pågående redigering innan verktyget körs."
    ),
    "name_field": (
        "Textfält med artnamnen. Svenska och vetenskapliga namn och synonymer fungerar, med "
        "eller utan auktor. Namn som slutar på sp. matchas mot släktet. Tomma rader hoppas "
        "över."
    ),
    "red_field": (
        "Fält som sätts till Ja om arten har någon av de valda rödlistekategorierna i "
        "Dyntaxa, annars Nej. Textfält får Ja/Nej och heltalsfält 1/0; har fältet en kodad "
        "domän används dess ja/nej-värden. Lämna tomt för att inte fylla i rödlistad."
    ),
    "prot_field": (
        "Fält som sätts till Ja om arten finns i Artdatabankens artlista Fridlysta arter "
        "(artskyddsförordningen), annars Nej. En underart eller varietet av en fridlyst art "
        "räknas som skyddad. Kräver SOS-nyckel. Lämna tomt för att inte fylla i skyddad."
    ),
    "inv_field": (
        "Fält som sätts till Ja om arten finns i någon av de valda artlistorna för invasiva "
        "arter, annars Nej. Kräver SOS-nyckel. Lämna tomt för att inte fylla i invasiv."
    ),
    "red_cats": (
        "Rödlistekategorier som räknas som rödlistad. Standard är RE, CR, EN, VU och NT. "
        "Kunskapsbrist (DD) ingår i Artdatabankens rödlista men är ingen hotkategori och är "
        "därför bortvald som standard. Gäller bara när ett fält för rödlistad är valt."
    ),
    "inv_lists": (
        "Artlistor som räknas som invasiv. Standard är EU:s förteckning och den nationella "
        "förteckningen. Alla främmande arter i Sverige är en mycket större lista (omkring "
        "5 000 taxa) som också innehåller arter som inte är invasiva. Gäller bara när ett "
        "fält för invasiv är valt."
    ),
    "min_similarity": (
        "Hur lik närmaste träff i Dyntaxa ska vara i procent när namnet inte finns exakt. "
        "Standard är 85. Lägre värde ger fler träffar och större risk för fel taxon; 100 "
        "kräver exakt namn. Varje närmaste träff skrivs i loggen så att den kan kontrolleras."
    ),
    "dyntaxa_key": (
        "Prenumerationsnyckel för Dyntaxa Taxon Service från api-portal.artdatabanken.se. "
        "Lämnas den tom används en sparad nyckel, miljövariabeln " + ENV_DYNTAXA + " eller "
        "SOS-nyckeln, som också fungerar mot Dyntaxa. Nyckeln sparas i projektets "
        "geoprocessing-historik om den skrivs här."
    ),
    "sos_key": (
        "Prenumerationsnyckel för Species Observation System, krävs bara för skyddad och "
        "invasiv. Lämnas den tom används en sparad nyckel eller miljövariabeln "
        + ENV_SOS + ". Nyckeln sparas i projektets geoprocessing-historik om den skrivs här."
    ),
    "save_keys": (
        "Sparar nycklarna i Windows Autentiseringshanteraren på den här datorn, efter att de "
        "har klarat ett testanrop. De används sedan när fälten lämnas tomma."
    ),
}


# =============================================================================
# Hjälpfunktioner: nycklar
# =============================================================================

def _saved_key(user):
    try:
        import keyring
        return keyring.get_password(CREDENTIAL_SERVICE, user) or ""
    except Exception:
        return ""


def _key(parameter, user, env):
    """Nyckel i ordningen: fältet, Autentiseringshanteraren, miljövariabeln."""
    value = (parameter.valueAsText or "").strip() if parameter is not None else ""
    return value or _saved_key(user) or os.environ.get(env, "").strip()


def _keys(p_dyntaxa, p_sos):
    """(Dyntaxa-nyckel, SOS-nyckel). SOS-nyckeln fungerar också mot Dyntaxa."""
    sos = _key(p_sos, CREDENTIAL_SOS, ENV_SOS)
    return _key(p_dyntaxa, CREDENTIAL_DYNTAXA, ENV_DYNTAXA) or sos, sos


def _missing_key_text(what, env):
    return ("Ingen {} angiven. Fyll i fältet, spara en nyckel med Spara nycklarna på den här "
            "datorn, eller sätt miljövariabeln {}. Nyckeln hämtas på "
            "api-portal.artdatabanken.se.".format(what, env))


def _remember_keys(p_dyntaxa, p_sos, need_sos, messages):
    """Spara nycklar som skrivits i fälten och klarat ett testanrop (annars ValueError)."""
    import keyring
    saved = []
    dyn = (p_dyntaxa.valueAsText or "").strip()
    sos = (p_sos.valueAsText or "").strip()
    if dyn:
        _get_json(DYNTAXA_API + "/taxa/search?searchString=abborre&searchFields=Both&pageSize=1",
                  dyn, "Dyntaxa")
        keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_DYNTAXA, dyn)
        saved.append("Dyntaxa")
    if sos and need_sos:
        _get_json(SOS_API + "/TaxonLists", sos, "SOS")
        keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_SOS, sos)
        saved.append("SOS")
    if saved:
        messages.addMessage("Nyckel för {} sparad i Windows Autentiseringshanteraren."
                            .format(" och ".join(saved)))


# =============================================================================
# Hjälpfunktioner: HTTP
# =============================================================================

def _http(request, what):
    """Skicka en begäran med omförsök vid 408, 429, 5xx och nätverksfel."""
    for attempt in range(HTTP_RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise ValueError("{} godkände inte nyckeln (HTTP {}). Kontrollera nyckeln och "
                                 "att prenumerationen omfattar {} på api-portal.artdatabanken.se."
                                 .format(what, exc.code, what))
            if exc.code not in (408, 429) and exc.code < 500:
                raise ValueError("{} svarade HTTP {}: {}".format(
                    what, exc.code, exc.read()[:300].decode("utf-8", "replace")))
            wait = int(exc.headers.get("Retry-After") or 0) or 2 ** attempt
        except OSError:
            wait = 2 ** attempt
        time.sleep(min(wait, 30))
    raise ValueError("{} svarar inte efter {} försök. Försök igen senare."
                     .format(what, HTTP_RETRIES))


def _get_json(url, key, what):
    req = urllib.request.Request(url, headers={"Ocp-Apim-Subscription-Key": key,
                                               "User-Agent": USER_AGENT})
    return json.loads(_http(req, what).decode("utf-8"))


# =============================================================================
# Hjälpfunktioner: namn och Dyntaxa
# =============================================================================

_AUTHOR = re.compile(r"^([A-Z][a-z-]+ [a-z-]+)( [A-Z(].*| \w+\.)$")


def _clean(name):
    """Namnet utan cf., frågetecken, avslutande sp./spp. och auktor."""
    s = re.sub(r"\b(?:cf|aff)\.", " ", str(name), flags=re.I).replace("?", " ")
    s = re.sub(r"\s+spp?\.?$", "", " ".join(s.split()), flags=re.I)
    return _AUTHOR.sub(r"\1", s).strip()


def _fold(text):
    return text.strip().lower().replace("é", "e").replace("×", "x")


def _recommended(t, category):
    return next((n["name"] for n in t["names"]
                 if n["category"]["value"] == category and n["isRecommended"]), "")


def _label(t):
    vet, sv = _recommended(t, "ScientificName"), _recommended(t, "SwedishName")
    return "{} ({})".format(vet, sv) if vet and sv else (vet or sv or str(t["taxonId"]))


def _redlist_abbreviation(t):
    m = re.search(r"\((\w+)\)\s*$", t.get("redlistCategory") or "")
    return m.group(1) if m else None


RANKS_ABOVE_SPECIES = {
    "Genus", "Subgenus", "Section", "Subsection", "Series", "Tribe", "Subtribe", "Family",
    "Subfamily", "Superfamily", "Order", "Suborder", "Superorder", "Class", "Subclass",
    "Superclass", "Phylum", "Subphylum", "Division", "Kingdom", "Subkingdom", "Domain"}


class NoMatch(Exception):
    """Ett namn som inte kan matchas. Skiljs från ValueError, som är fel som stoppar körningen."""


class Dyntaxa:
    """Uppslag av artnamn i Dyntaxa med reglerna i modulens dokumentation."""

    def __init__(self, key, min_similarity, need_parents):
        self.key = key
        self.min_similarity = min_similarity / 100.0
        self.need_parents = need_parents
        self._search_memo = {}
        self._parents_memo = {}

    def _search(self, text, page=1):
        k = (text, page)
        if k not in self._search_memo:
            url = DYNTAXA_API + "/taxa/search?" + urllib.parse.urlencode({
                "searchString": text, "searchFields": "Both", "pageSize": PAGE_SIZE, "page": page})
            self._search_memo[k] = _get_json(url, self.key, "Dyntaxa")
        return self._search_memo[k]

    def parents(self, taxon_id):
        if taxon_id not in self._parents_memo:
            d = _get_json("{}/taxa/{}/parentids".format(DYNTAXA_API, taxon_id), self.key, "Dyntaxa")
            self._parents_memo[taxon_id] = set(d["taxonIds"])
        return self._parents_memo[taxon_id]

    def _exact(self, text):
        """Accepterade taxa som har exakt detta namn (svenskt, vetenskapligt eller synonym)."""
        key = _fold(text)
        hits = {}
        for page in range(1, MAX_PAGES + 1):
            d = self._search(text, page)
            for t in d["data"]:
                if t["status"]["value"] == "Accepted" and any(
                        _fold(n["name"]) == key for n in t["names"]):
                    hits[t["taxonId"]] = t
            if hits or page >= d["totalNrOfPages"]:
                break
        return list(hits.values())

    def _pick(self, hits, text):
        """Ett taxon av flera med samma namn, eller None om det är oklart."""
        key = _fold(text)
        rec = [t for t in hits if any(_fold(n["name"]) == key and n["isRecommended"]
                                      for n in t["names"])]
        if len(rec) == 1:
            return rec[0]
        return self._species_over_subspecies(hits)

    def _species_over_subspecies(self, taxa):
        """Arten när alla taxa är arten och dess underarter eller varieteter, annars None."""
        species = [t for t in taxa if t["category"]["value"] == "Species"]
        if len(species) == 1:
            sid = species[0]["taxonId"]
            if all(t["taxonId"] == sid or sid in self.parents(t["taxonId"]) for t in taxa):
                return species[0]
        return None

    def _nearest(self, text):
        """(taxon, matchat namn, likhet) eller ValueError med orsaken."""
        q = _fold(text)
        best = {}
        ranked = []
        tried = set()
        for k in (max(4, len(q) // 2), max(4, len(q) // 3)):
            if k >= len(q):
                continue
            for frag in (text[:k], text[-k:]):
                if frag in tried:
                    continue
                tried.add(frag)
                for t in self._search(frag)["data"]:
                    if t["status"]["value"] != "Accepted":
                        continue
                    for n in t["names"]:
                        if n["category"]["value"] not in ("SwedishName", "ScientificName"):
                            continue
                        r = difflib.SequenceMatcher(None, q, _fold(n["name"])).ratio()
                        if r > best.get(t["taxonId"], (0,))[0]:
                            best[t["taxonId"]] = (r, t, n["name"])
            ranked = sorted(best.values(), key=lambda x: -x[0])
            if ranked and ranked[0][0] >= self.min_similarity:
                break
        if not ranked or ranked[0][0] < self.min_similarity:
            raise NoMatch("ingen träff i Dyntaxa" + (
                " (närmast: {} {:.0f} %)".format(ranked[0][2], ranked[0][0] * 100)
                if ranked else ""))
        r, t, name = ranked[0]
        tied = [x[1] for x in ranked if r - x[0] < AMBIGUITY_MARGIN]
        if len(tied) > 1:
            t = self._species_over_subspecies(tied)
            if t is None:
                raise NoMatch("flera taxa är lika nära: " + " och ".join(
                    _label(x) for x in tied[:3]))
        return t, name, r

    def resolve(self, name):
        """
        Slår upp ett namn. Returnerar en post med ok, och för träff taxonId, label, category,
        redlist (förkortning eller None), how (exakt eller närmaste), sim och ids (taxonet
        och dess föräldrar när de behövs); för miss reason.
        """
        text = _clean(name)
        if not text:
            return {"ok": False, "reason": "namnet är tomt efter rensning"}
        try:
            hits = self._exact(text)
            if len(hits) == 1:
                t, how, sim = hits[0], "exakt", 1.0
            elif hits:
                t = self._pick(hits, text)
                if t is None:
                    raise NoMatch("namnet matchar flera taxa: " +
                                  ", ".join(_label(h) for h in hits[:5]))
                how, sim = "exakt", 1.0
            else:
                t, matched, sim = self._nearest(text)
                how = "närmaste"
            ids = (self.parents(t["taxonId"]) | {t["taxonId"]}) if self.need_parents else set()
        except NoMatch as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "taxonId": t["taxonId"], "label": _label(t),
                "category": t["category"]["name"], "redlist": _redlist_abbreviation(t),
                "how": how, "sim": sim, "ids": ids,
                "above_species": t["category"]["value"] in RANKS_ABOVE_SPECIES}


def _list_ids(list_id, key):
    """Taxon-id i en artlista i SOS."""
    return {x["id"] for x in _get_json("{}/TaxonLists/{}/Taxa".format(SOS_API, list_id),
                                       key, "SOS")}


# =============================================================================
# Hjälpfunktioner: fält
# =============================================================================

def _workspace_path(dataset):
    """Sökväg till filgeodatabasen som innehåller datasetet, eller None."""
    path = os.path.dirname(arcpy.Describe(dataset).catalogPath)
    while path and not path.lower().endswith((".gdb", ".sde")):
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent
    return path or None


def _pick_code(domain, words, what):
    for code, desc in domain.codedValues.items():
        if str(code).strip().lower() in words or str(desc).strip().lower() in words:
            return code
    raise ValueError("Domänen {} har inget värde för {} (väntar ja/nej, 1/0, yes/no eller "
                     "true/false).".format(domain.name, what))


def _yes_no(dataset, field):
    """(värde för Ja, värde för Nej) för ett fält, med hänsyn till typ, längd och domän."""
    if field.type in ("SmallInteger", "Integer"):
        yes, no = 1, 0
    elif field.type == "String":
        yes, no = "Ja", "Nej"
    else:
        raise ValueError("Fältet {} har typen {}. Använd ett textfält eller ett heltalsfält."
                         .format(field.name, field.type))
    if field.domain:
        workspace = _workspace_path(dataset)
        domain = next((d for d in arcpy.da.ListDomains(workspace) if d.name == field.domain),
                      None) if workspace else None
        if domain is not None and domain.domainType == "CodedValue":
            yes = _pick_code(domain, YES_WORDS, "Ja")
            no = _pick_code(domain, NO_WORDS, "Nej")
    need = max(len(str(yes)), len(str(no)))
    if field.type == "String" and field.length < need:
        raise ValueError("Textfältet {} är för kort för {} och {} (längd {}, minst {} behövs)."
                         .format(field.name, yes, no, field.length, need))
    return yes, no


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 90:
        return "{} s".format(seconds)
    return "{} min".format(round(seconds / 60))


# =============================================================================
# Körning
# =============================================================================

def _run(in_table, name_field, red_field=None, prot_field=None, inv_field=None,
         red_cats=None, inv_lists=None, min_similarity=DEFAULT_MIN_SIMILARITY,
         dyntaxa_key="", sos_key="", messages=None, workers=WORKERS):
    """Hela körningen. Returnerar en ordbok med räknare, för test och loggning."""
    flag_fields = {"red": red_field, "prot": prot_field, "inv": inv_field}
    chosen = {k: v for k, v in flag_fields.items() if v}
    if not chosen:
        raise ValueError("Välj minst ett fält att fylla i: rödlistad, skyddad eller invasiv.")
    if len({name_field, *chosen.values()}) != len(chosen) + 1:
        raise ValueError("Artnamnsfältet och fälten för rödlistad, skyddad och invasiv måste "
                         "vara olika fält.")
    need_sos = bool(prot_field or inv_field)
    if not dyntaxa_key:
        raise ValueError(_missing_key_text("Dyntaxa-nyckel", ENV_DYNTAXA))
    if need_sos and not sos_key:
        raise ValueError(_missing_key_text("SOS-nyckel", ENV_SOS) +
                         " Den krävs för skyddad och invasiv.")
    cats = {RED_CATEGORIES[c] for c in (red_cats or DEFAULT_RED)} if red_field else set()
    lists = [INVASIVE_LISTS[c] for c in (inv_lists or DEFAULT_INVASIVE)] if inv_field else []

    fields = {f.name: f for f in arcpy.ListFields(in_table)}
    for name in (name_field, *chosen.values()):
        if name not in fields:
            raise ValueError("Fältet {} finns inte i indata.".format(name))
    if fields[name_field].type != "String":
        raise ValueError("Artnamnsfältet {} måste vara ett textfält.".format(name_field))
    values = {k: _yes_no(in_table, fields[v]) for k, v in chosen.items()}

    # Steg 1: läs namnen. Lagret avgör vilka rader: urval och definitionsfråga gäller.
    messages.addMessage("Steg 1 av 4: läser artnamn")
    counts = {}
    n_rows = n_blank = 0
    with arcpy.da.SearchCursor(in_table, [name_field]) as cur:
        for (value,) in cur:
            n_rows += 1
            text = (value or "").strip()
            if not text:
                n_blank += 1
                continue
            counts[text] = counts.get(text, 0) + 1
    messages.addMessage("    {} rader, {} unika namn, {} utan namn".format(
        n_rows, len(counts), n_blank))
    if not counts:
        raise ValueError("Inga artnamn att slå upp i {}.".format(name_field))

    # Steg 2: artlistor i SOS
    protected = set()
    invasive = set()
    if need_sos:
        messages.addMessage("Steg 2 av 4: hämtar artlistor från SOS")
        if prot_field:
            protected = _list_ids(LIST_PROTECTED, sos_key)
            messages.addMessage("    Fridlysta arter: {} taxa".format(len(protected)))
        for list_id in lists:
            ids = _list_ids(list_id, sos_key)
            invasive |= ids
            messages.addMessage("    Artlista {}: {} taxa".format(list_id, len(ids)))
    else:
        messages.addMessage("Steg 2 av 4: inga artlistor behövs")

    # Steg 3: slå upp namnen
    resolver = Dyntaxa(dyntaxa_key, min_similarity, need_parents=need_sos)
    names = sorted(counts)
    results = {}
    t0 = time.time()
    arcpy.SetProgressor("step", "Slår upp namn i Dyntaxa", 0, len(names), 1)
    messages.addMessage("Steg 3 av 4: slår upp {} namn i Dyntaxa".format(len(names)))
    last_log = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        try:
            futures = {pool.submit(resolver.resolve, n): n for n in names}
            for done, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                results[futures[fut]] = fut.result()
                text = "Slår upp namn i Dyntaxa: {} av {}".format(done, len(names))
                if done >= 5 and time.time() - t0 > 5:
                    text += ", ca {} kvar".format(
                        _fmt_duration((time.time() - t0) / done * (len(names) - done)))
                arcpy.SetProgressorPosition(done)
                arcpy.SetProgressorLabel(text)
                if time.time() - last_log > 30:
                    messages.addMessage("    " + text)
                    last_log = time.time()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    # Steg 4: skriv fälten
    messages.addMessage("Steg 4 av 4: skriver {} fält".format(len(chosen)))
    arcpy.SetProgressor("default", "Skriver fält")
    tally = {k: [0, 0] for k in chosen}
    updated = 0
    with arcpy.da.UpdateCursor(in_table, [name_field] + list(chosen.values())) as cur:
        for row in cur:
            res = results.get((row[0] or "").strip())
            if not res or not res["ok"]:
                continue
            out = [row[0]]
            for i, key in enumerate(chosen, 1):
                if key == "red":
                    flag = res["redlist"] in cats
                elif key == "prot":
                    flag = bool(res["ids"] & protected)
                else:
                    flag = bool(res["ids"] & invasive)
                tally[key][0 if flag else 1] += 1
                out.append(values[key][0 if flag else 1])
            cur.updateRow(out)
            updated += 1

    # Sammanfattning och varningar
    approx = {n: r for n, r in results.items() if r["ok"] and r["how"] == "närmaste"}
    missed = {n: r for n, r in results.items() if not r["ok"]}
    above = {n: r for n, r in results.items() if r["ok"] and r["above_species"]}
    labels = {"red": "Rödlistad", "prot": "Skyddad", "inv": "Invasiv"}
    messages.addMessage("Klart: {} av {} rader uppdaterade".format(updated, n_rows))
    for key, (ja, nej) in tally.items():
        messages.addMessage("    {} ({}): {} Ja, {} Nej".format(
            labels[key], chosen[key], ja, nej))
    if approx:
        messages.addMessage("{} namn matchades till närmaste träff i Dyntaxa. Kontrollera:"
                            .format(len(approx)))
        for n in sorted(approx)[:MAX_LISTED]:
            messages.addMessage("    '{}' -> {} ({:.0f} %), {} rader".format(
                n, approx[n]["label"], approx[n]["sim"] * 100, counts[n]))
        if len(approx) > MAX_LISTED:
            messages.addMessage("    ... och {} till".format(len(approx) - MAX_LISTED))
    if above:
        messages.addWarningMessage(
            "{} namn är bestämda högre än art ({}). Ja/Nej gäller då taxonet på den nivån, "
            "inte dess arter.".format(len(above), ", ".join(sorted(above)[:5]) +
                                      (" ..." if len(above) > 5 else "")))
    if missed:
        rows = sum(counts[n] for n in missed)
        messages.addWarningMessage(
            "{} namn ({} rader) kunde inte matchas i Dyntaxa. Deras fält lämnades oförändrade:"
            .format(len(missed), rows))
        for n in sorted(missed)[:MAX_LISTED]:
            messages.addWarningMessage("    '{}' ({} rader): {}".format(
                n, counts[n], missed[n]["reason"]))
        if len(missed) > MAX_LISTED:
            messages.addWarningMessage("    ... och {} till".format(len(missed) - MAX_LISTED))
    if n_blank:
        messages.addWarningMessage("{} rader saknar artnamn och lämnades oförändrade."
                                   .format(n_blank))
    return {"rows": n_rows, "updated": updated, "blank": n_blank, "unique": len(names),
            "exact": sum(1 for r in results.values() if r["ok"] and r["how"] == "exakt"),
            "approx": {n: r["label"] for n, r in approx.items()},
            "missed": {n: r["reason"] for n, r in missed.items()}, "tally": tally}


# =============================================================================
# Toolbox
# =============================================================================

def _write_tool_metadata(tool_cls, toolbox_alias):
    """
    Skriv verktygets metadatafil med parameterförklaringar från TOOLTIPS.

    Pro läser verktygstipsen i dialogen från <verktygslåda>.<verktyg>.pyt.xml (elementet
    dialogReference per parameter). Det finns inget attribut på arcpy.Parameter för detta.
    Filen skrivs bara om innehållet har ändrats.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    toolbox = os.path.splitext(os.path.basename(__file__))[0]
    path = os.path.join(here, "{}.{}.pyt.xml".format(toolbox, tool_cls.__name__))

    def html(text):
        body = escape(text).replace("\n", "</SPAN></P><P><SPAN>")
        return escape('<DIV STYLE="text-align:Left;"><P><SPAN>{}</SPAN></P></DIV>'.format(body))

    tool = tool_cls()
    params = []
    for p in tool.getParameterInfo():
        tip = TOOLTIPS.get(p.name)
        if not tip:
            continue
        params.append(
            '<param name="{n}" displayname="{d}" type="{t}" direction="{r}">'
            "<dialogReference>{h}</dialogReference>"
            "<pythonReference>{h}</pythonReference></param>".format(
                n=p.name, d=escape(p.displayName, {'"': "&quot;"}),
                t=p.parameterType, r=p.direction, h=html(tip)))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<metadata xml:lang="sv"><Esri><ArcGISFormat>1.0</ArcGISFormat></Esri>'
        '<tool name="{name}" displayname="{label}" toolboxalias="{alias}" xmlns="">'
        "<parameters>{params}</parameters><summary>{summary}</summary></tool>"
        "<dataIdInfo><idCitation><resTitle>{label}</resTitle></idCitation>"
        "<idAbs>{summary}</idAbs></dataIdInfo></metadata>\n"
    ).format(name=tool_cls.__name__, label=escape(tool.label), alias=toolbox_alias,
             params="".join(params), summary=html(TOOL_SUMMARY))
    try:
        with open(path, encoding="utf-8") as fh:
            if fh.read() == xml:
                return
    except OSError:
        pass
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(xml)
    except OSError:
        pass  # skrivskyddad plats: verktyget fungerar ändå, bara utan verktygstips


class Toolbox:
    def __init__(self):
        self.label = "Flagga arter från Dyntaxa"
        self.alias = "flagga_arter_dyntaxa"
        self.tools = [FlaggaArter]
        _write_tool_metadata(FlaggaArter, self.alias)


class FlaggaArter:
    def __init__(self):
        self.label = "Flagga arter (rödlistad, skyddad, invasiv)"
        self.description = TOOL_SUMMARY
        self.canRunInBackground = False

    def getParameterInfo(self):
        def param(name, display, datatype, ptype="Optional", category=None,
                  direction="Input"):
            return arcpy.Parameter(displayName=display, name=name, datatype=datatype,
                                   parameterType=ptype, direction=direction,
                                   category=category)

        p_in = param("in_table", "Indatatabell", "GPTableView", "Required")
        p_name = param("name_field", "Artnamnsfält", "Field", "Required")
        p_name.filter.list = ["Text"]
        p_name.parameterDependencies = [p_in.name]
        flag = []
        for name, display in (("red_field", "Fält för rödlistad"),
                              ("prot_field", "Fält för skyddad"),
                              ("inv_field", "Fält för invasiv")):
            p = param(name, display, "Field")
            p.filter.list = ["Text", "Short", "Long"]
            p.parameterDependencies = [p_in.name]
            flag.append(p)
        p_out = param("out_table", "Uppdaterad tabell", "GPTableView", "Derived",
                      direction="Output")
        p_out.parameterDependencies = [p_in.name]

        cat = "Alternativ"
        p_red = param("red_cats", "Rödlistekategorier som räknas som rödlistad",
                      "GPString", category=cat)
        p_red.multiValue = True
        p_red.filter.type = "ValueList"
        p_red.filter.list = list(RED_CATEGORIES)
        p_red.values = DEFAULT_RED
        p_inv = param("inv_lists", "Artlistor som räknas som invasiv", "GPString",
                      category=cat)
        p_inv.multiValue = True
        p_inv.filter.type = "ValueList"
        p_inv.filter.list = list(INVASIVE_LISTS)
        p_inv.values = DEFAULT_INVASIVE
        p_sim = param("min_similarity", "Minsta likhet för närmaste träff (%)", "GPLong",
                      category=cat)
        p_sim.filter.type = "Range"
        p_sim.filter.list = [50, 100]
        p_sim.value = DEFAULT_MIN_SIMILARITY

        cat = "Inloggning"
        p_dyn = param("dyntaxa_key", "Dyntaxa-nyckel", "GPStringHidden", category=cat)
        p_sos = param("sos_key", "SOS-nyckel", "GPStringHidden", category=cat)
        p_save = param("save_keys", "Spara nycklarna på den här datorn", "GPBoolean",
                       category=cat)
        p_save.value = False
        return [p_in, p_name, *flag, p_out, p_red, p_inv, p_sim, p_dyn, p_sos, p_save]

    def isLicensed(self):
        return True

    @staticmethod
    def _p(parameters):
        return {p.name: p for p in parameters}

    def updateParameters(self, parameters):
        p = self._p(parameters)
        p["red_cats"].enabled = bool(p["red_field"].value)
        p["inv_lists"].enabled = bool(p["inv_field"].value)

    def updateMessages(self, parameters):
        p = self._p(parameters)
        chosen = [n for n in ("red_field", "prot_field", "inv_field") if p[n].value]
        if p["in_table"].value and not chosen:
            p["red_field"].setErrorMessage(
                "Välj minst ett fält att fylla i: rödlistad, skyddad eller invasiv.")
        names = [p[n].valueAsText for n in ("name_field", *chosen) if p[n].value]
        if len(set(names)) != len(names):
            for n in chosen:
                p[n].setErrorMessage("Fälten måste vara olika, och olika artnamnsfältet.")
        if p["red_field"].value and not p["red_cats"].values:
            p["red_cats"].setErrorMessage("Välj minst en rödlistekategori.")
        if p["inv_field"].value and not p["inv_lists"].values:
            p["inv_lists"].setErrorMessage("Välj minst en artlista.")
        dyn, sos = _keys(p["dyntaxa_key"], p["sos_key"])
        if not dyn:
            p["dyntaxa_key"].setErrorMessage(_missing_key_text("Dyntaxa-nyckel", ENV_DYNTAXA))
        if (p["prot_field"].value or p["inv_field"].value) and not sos:
            p["sos_key"].setErrorMessage(_missing_key_text("SOS-nyckel", ENV_SOS) +
                                         " Den krävs för skyddad och invasiv.")

    def execute(self, parameters, messages):
        p = self._p(parameters)
        error = None
        try:
            dyn, sos = _keys(p["dyntaxa_key"], p["sos_key"])
            if p["save_keys"].value:
                _remember_keys(p["dyntaxa_key"], p["sos_key"],
                               bool(p["prot_field"].value or p["inv_field"].value), messages)
            _run(p["in_table"].value, p["name_field"].valueAsText,
                 p["red_field"].valueAsText, p["prot_field"].valueAsText,
                 p["inv_field"].valueAsText,
                 red_cats=list(p["red_cats"].values or []),
                 inv_lists=list(p["inv_lists"].values or []),
                 min_similarity=p["min_similarity"].value or DEFAULT_MIN_SIMILARITY,
                 dyntaxa_key=dyn, sos_key=sos, messages=messages)
            p["out_table"].value = p["in_table"].value
        except ValueError as exc:
            error = str(exc)
        finally:
            arcpy.ResetProgressor()
        if error:
            messages.addErrorMessage(error)
            raise arcpy.ExecuteError
