"""Citation extraction, the doctrine graph and validity status."""

from datetime import date

from langchain_core.documents import Document

from hacienda_gpt.doctrine.citas import Documento, extract_citations, identify_document, parse_date
from hacienda_gpt.doctrine.construccion import build_graph
from hacienda_gpt.doctrine.grafo import Arista, DoctrineGraph
from hacienda_gpt.doctrine.relaciones import leer_heuristica, leer_llm

PASAJE = (
    "En este sentido se pronuncia la consulta V1234-19 y la resolución del TEAC de 25 de junio de 2019 "
    "(RG 00/1234/2017). No obstante, la sentencia del Tribunal Supremo de 3 de junio de 2020 (rec. cas. 1234/2019) "
    "ha fijado un criterio distinto, igual que la STS 1.456/2021, de 13 de diciembre (recurso de casación núm. "
    "4567/2020), y la STC 182/2021. ECLI:ES:TS:2021:4500. El expediente 30/1234/2017 no guarda relación."
)


def test_citations_are_extracted_with_canonical_keys_and_aliases() -> None:
    by_key = {c.clave: c for c in extract_citations(PASAJE)}

    assert set(by_key) == {
        "DGT:V1234-19",
        "TEAC:00/01234/2017",
        "TS:REC:1234/2019",
        "TS:REC:4567/2020",
        "TC:182/2021",
        "TS:ECLI:ES:TS:2021:4500",
    }
    # "30/1234/2017" is not near any TEAC wording… but shares the passage here, so only the RG one is keyed.
    assert by_key["TEAC:00/01234/2017"].fecha == date(2019, 6, 25)
    first, second = by_key["TS:REC:1234/2019"], by_key["TS:REC:4567/2020"]
    assert first.fecha == date(2020, 6, 3) and first.alias == ()  # identifiers of the NEXT ruling are not borrowed
    assert second.alias == ("TS:NUM:1456/2021",)
    assert "criterio distinto" in first.contexto
    # The sentence is what is said about THIS document; neighbours are excluded.
    assert first.frase.startswith("No obstante, la sentencia del Tribunal Supremo") and "V1234-19" not in first.frase
    assert (
        by_key["DGT:V1234-19"].frase.startswith("En este sentido")
        and "criterio distinto" not in by_key["DGT:V1234-19"].frase
    )

    # A document does not cite itself, and a bare mention of the Court is not a citation.
    assert "DGT:V1234-19" not in {c.clave for c in extract_citations(PASAJE, propias={"DGT:V1234-19"})}
    assert extract_citations("Según reiterada jurisprudencia del Tribunal Supremo, procede la deducción.") == []
    assert extract_citations("El importe fue de 12/3456/2020 euros según factura.") == []


def test_dates_are_parsed_in_the_forms_the_corpus_uses() -> None:
    assert parse_date("de 3 de junio de 2020") == date(2020, 6, 3)
    assert parse_date("fecha 03/06/2020") == date(2020, 6, 3)
    assert parse_date("2020-06-03T00:00:00") == date(2020, 6, 3)
    assert parse_date("31 de febrero de 2020") is None and parse_date("sin fecha") is None


def test_corpus_documents_are_identified_from_their_metadata() -> None:
    dgt = identify_document(
        {"source": "data/html/x/dgt-vinculantes/2019/V1234-19.html", "fecha_resolucion": "2019-05-10"}
    )
    assert (dgt.clave, dgt.tipo, dgt.vinculante, dgt.fecha) == ("DGT:V1234-19", "DGT", True, date(2019, 5, 10))

    teac = identify_document({"source_authority": "TEAC", "numero_resolucion": "00/1234/2017", "vinculante": True})
    assert teac.clave == "TEAC:00/01234/2017" and teac.vinculante is True

    ts = identify_document(
        {
            "source_authority": "TS_SALA_TERCERA",
            "ecli": "ES:TS:2020:1500",
            "roj": "STS 1500/2020",
            "numero_recurso": "1234/2019",
            "fecha_resolucion": "2020-06-03",
            "fija_doctrina": True,
        }
    )
    assert ts.clave == "TS:ECLI:ES:TS:2020:1500" and ts.fija_doctrina
    assert ts.alias == ["TS:ROJ:1500/2020", "TS:REC:1234/2019"]

    tc = identify_document({"source_file": "boe-jurisprudencia-tc/BOE-A-2021-19511.html", "title": "STC 182/2021"})
    assert tc.clave == "TC:182/2021"
    # Statutes and AEAT pages are cited, but they are not nodes with a validity status.
    assert identify_document({"source_url": "https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764"}) is None
    assert identify_document({"source_authority": "TS"}) is None


def test_heuristic_reading_is_conservative() -> None:
    assert leer_heuristica("lo que supone un cambio de criterio respecto de la consulta V1234-19").relacion == "supera"
    assert leer_heuristica("este Tribunal no comparte el criterio de la resolución citada").relacion == "contradice"
    assert leer_heuristica("criterio que debe matizarse en los supuestos de").relacion == "matiza"
    assert leer_heuristica("en el mismo sentido, la consulta V1234-19").relacion == "confirma"
    assert leer_heuristica("la recurrente invoca la consulta V1234-19 en su escrito").relacion == "cita"


class _Provider:
    def __init__(self, *responses) -> None:
        self.responses, self.requests = list(responses), []

    def complete_json(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def test_llm_reading_needs_a_quote_that_is_really_in_the_passage() -> None:
    contexto = "La Sala declara que el criterio de la consulta V1234-19 no es conforme a Derecho."
    good = _Provider({"relacion": "supera", "cita_literal": "no es conforme a  Derecho", "motivo": "lo rechaza"})
    lectura = leer_llm(good, origen="STS", destino="consulta DGT V1234-19", contexto=contexto)
    assert (lectura.relacion, lectura.metodo) == ("supera", "llm") and lectura.confianza > 0.8
    assert "consulta DGT V1234-19" in good.requests[0]["user"]

    invented = _Provider({"relacion": "supera", "cita_literal": "anula expresamente la consulta", "motivo": "x"})
    assert leer_llm(invented, origen="STS", destino="x", contexto=contexto) is None
    neutral = _Provider({"relacion": "cita", "cita_literal": "", "motivo": ""})
    assert leer_llm(neutral, origen="STS", destino="x", contexto=contexto).relacion == "cita"
    assert (
        leer_llm(
            _Provider({"relacion": "deroga", "cita_literal": "", "motivo": ""}),
            origen="a",
            destino="b",
            contexto=contexto,
        )
        is None
    )


def _doc(clave: str, tipo: str, fecha: date | None = None, **kwargs) -> Documento:
    return Documento(clave, tipo, fecha=fecha, **kwargs)


def _edge(origen: str, destino: str, relacion: str, confianza: float = 0.8) -> Arista:
    return Arista(origen=origen, destino=destino, relacion=relacion, confianza=confianza, metodo="manual")


def test_validity_status_follows_rank_and_date() -> None:
    g = DoctrineGraph(":memory:")
    g.upsert(_doc("DGT:V1234-19", "DGT", date(2019, 5, 10)), en_corpus=True)
    g.upsert(_doc("TS:REC:1234/2019", "TS", date(2020, 6, 3)), en_corpus=True)
    g.upsert(_doc("TEAC:00/00100/2020", "TEAC", date(2020, 9, 1)), en_corpus=True)
    g.upsert(_doc("DGT:V0001-21", "DGT", date(2021, 1, 15)), en_corpus=True)

    assert g.vigencia("DGT:V9999-99").estado == "desconocido"
    assert g.vigencia("DGT:V1234-19").estado == "vigente"

    # A later consulta of the same rank qualifies it.
    g.add_edge(_edge("DGT:V0001-21", "DGT:V1234-19", "matiza"))
    assert g.vigencia("DGT:V1234-19").estado == "matizada"

    # The Supreme Court (higher rank, later) overrules it: that wins.
    g.add_edge(_edge("TS:REC:1234/2019", "DGT:V1234-19", "supera"))
    status = g.vigencia("DGT:V1234-19")
    assert status.estado == "superada"
    assert status.resumen == "Superada por STS rec. 1234/2019 (03/06/2020)."
    assert [a.origen for a in status.motivos] == ["TS:REC:1234/2019"]

    # A consulta "overruling" a Supreme Court ruling cannot: the matter is disputed, not settled.
    g.add_edge(_edge("DGT:V0001-21", "TS:REC:1234/2019", "supera"))
    disputed = g.vigencia("TS:REC:1234/2019")
    assert disputed.estado == "discutida" and "consulta DGT V0001-21" in disputed.resumen
    # …and the consulta that goes against the higher court is itself flagged.
    g.add_edge(_edge("DGT:V0001-21", "TEAC:00/00100/2020", "contradice"))
    assert g.vigencia("DGT:V0001-21").estado == "discutida"
    assert g.vigencia("TEAC:00/00100/2020").estado == "discutida"


def test_aliases_merge_and_edges_never_downgrade() -> None:
    g = DoctrineGraph(":memory:")
    # First seen as a bare citation by appeal number…
    g.upsert(_doc("TS:REC:1234/2019", "TS"), en_corpus=False)
    g.upsert(_doc("DGT:V1234-19", "DGT", date(2019, 5, 10)), en_corpus=True)
    g.add_edge(_edge("DGT:V1234-19", "TS:REC:1234/2019", "confirma"))
    # …then ingested with its ECLI: same node, now in the corpus, edges kept.
    canonical = g.upsert(
        _doc("TS:ECLI:ES:TS:2020:1500", "TS", date(2020, 6, 3), alias=["TS:REC:1234/2019"], titulo="STS 3-6-2020"),
        en_corpus=True,
    )
    assert canonical == "TS:REC:1234/2019" == g.resolve("TS:ECLI:ES:TS:2020:1500")
    node = g.get("TS:ECLI:ES:TS:2020:1500")
    assert node.en_corpus and node.fecha == date(2020, 6, 3) and node.titulo == "STS 3-6-2020"
    assert [a.origen for a in g.entrantes("TS:ECLI:ES:TS:2020:1500")] == ["DGT:V1234-19"]

    g.add_edge(_edge("DGT:V1234-19", "TS:REC:1234/2019", "cita", 0.9))  # neutral never overwrites typed
    g.add_edge(_edge("DGT:V1234-19", "TS:REC:1234/2019", "matiza", 0.3))  # nor does a weaker reading
    assert g.entrantes("TS:REC:1234/2019")[0].relacion == "confirma"
    g.add_edge(_edge("DGT:V1234-19", "DGT:V1234-19", "supera"))  # self-edges are ignored
    assert g.stats()["aristas"] == 1


def _chunks() -> list[Document]:
    return [
        Document(
            page_content="Consulta sobre la exención. Se concluye que no procede la exención por reinversión.",
            metadata={
                "source_file": "dgt-vinculantes/2019/V1234-19.html",
                "num_consulta": "V1234-19",
                "fecha_resolucion": "2019-05-10",
            },
        ),
        Document(
            page_content="FUNDAMENTOS. La recurrente invoca la consulta V0777-18 en su demanda.",
            metadata={
                "source_file": "ts/STS_1500_2020.html",
                "source_authority": "TS_SALA_TERCERA",
                "ecli": "ES:TS:2020:1500",
                "numero_recurso": "1234/2019",
                "fecha_resolucion": "2020-06-03",
            },
        ),
        Document(
            page_content="Esta Sala declara que procede la exención, lo que supone un cambio de criterio respecto de la consulta V1234-19.",
            metadata={
                "source_file": "ts/STS_1500_2020.html",
                "source_authority": "TS_SALA_TERCERA",
                "ecli": "ES:TS:2020:1500",
                "numero_recurso": "1234/2019",
                "fecha_resolucion": "2020-06-03",
            },
        ),
        Document(
            page_content="En el mismo sentido que la sentencia del Tribunal Supremo de 3 de junio de 2020 (rec. 1234/2019), procede la exención. Véase también la STS de 5 de mayo de 2021 (rec. 999/2020).",
            metadata={
                "source_file": "teac/00_00100_2021.html",
                "source_authority": "TEAC",
                "numero_resolucion": "00/100/2021",
                "fecha_resolucion": "2021-02-01",
            },
        ),
        Document(page_content="Artículo 38. Reinversión.", metadata={"source_file": "boe/BOE-A-2006-20764.html"}),
    ]


def test_graph_is_built_from_corpus_chunks_and_flags_superseded_doctrine() -> None:
    g = DoctrineGraph(":memory:")
    report = build_graph(_chunks(), g)

    assert (report.documentos, report.documentos_sin_identidad) == (3, 1)
    assert report.relaciones == {"cita": 2, "supera": 1, "confirma": 1}
    # The TEAC cited the ruling by appeal number; it resolves to the ingested ECLI node.
    assert g.resolve("TS:REC:1234/2019") == "TS:ECLI:ES:TS:2020:1500"
    assert g.vigencia("DGT:V1234-19").estado == "superada"
    assert g.vigencia("TS:REC:1234/2019").estado == "vigente"

    missing = g.mas_citadas_fuera_del_corpus("TS")
    assert [(n.clave, count) for n, count in missing] == [("TS:REC:999/2020", 1)]
    assert missing[0][0].fecha == date(2021, 5, 5)

    # Re-running is idempotent.
    build_graph(_chunks(), g)
    assert g.stats()["aristas"] == 4 and g.stats()["nodos_en_corpus"] == 3


def test_llm_budget_is_spent_only_where_it_matters() -> None:
    g = DoctrineGraph(":memory:")
    provider = _Provider(
        {"relacion": "cita", "cita_literal": "", "motivo": "alegación de parte"},
        {"relacion": "supera", "cita_literal": "cambio de criterio", "motivo": "lo dice"},
        {"relacion": "confirma", "cita_literal": "En el mismo sentido", "motivo": "sigue"},
    )
    report = build_graph(_chunks(), g, provider=provider, max_llm_calls=3)

    # Court → consulta (×2) and TEAC → court flagged by the heuristic; the plain TEAC → court mention too,
    # but the budget of 3 is respected.
    assert report.llamadas_llm == 3 and len(provider.requests) == 3
    assert {a.metodo for a in g.entrantes("DGT:V1234-19")} == {"llm"}
    assert g.vigencia("DGT:V1234-19").estado == "superada"
