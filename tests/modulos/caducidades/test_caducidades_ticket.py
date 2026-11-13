"""Reading a purchase from a receipt: only values with a literal passage in the document survive."""

from typing import Any

from hacienda_gpt.modulos.caducidades.ticket import fechas_en, importes_en, leer_ticket, verificar_propuesta

TIQUE = """ELECTRODOMÉSTICOS EL PUENTE S.L.
C/ Mayor 1 - Madrid
Fecha: 08/11/2026  Hora: 12:31
1 x LAVADORA CARGA FRONTAL 8KG      449,00
SUBTOTAL                            449,00
TOTAL EUROS                         449,00
Forma de pago: tarjeta
"""


def test_dates_and_amounts_are_read_in_spanish_formats() -> None:
    assert [d.isoformat() for d in fechas_en("08/11/2026, 3 de marzo de 2026, 2026-01-02 y 31/02/2026")] == [
        "2026-01-02",
        "2026-11-08",
        "2026-03-03",
    ]
    assert [str(i) for i in importes_en("1.234,56 y 49,90 y 12.50")] == ["1234.56", "49.90", "12.50"]


def test_without_a_model_the_date_and_total_are_proposed_with_their_line() -> None:
    propuesta = verificar_propuesta(TIQUE)
    assert propuesta.valor("fecha_compra") == "2026-11-08"
    assert propuesta.valor("importe") == "449.00"
    assert next(c for c in propuesta.campos if c.campo == "importe").evidencia == "TOTAL EUROS 449,00"
    assert any("descripción" in a for a in propuesta.avisos)


def test_model_proposal_is_kept_only_with_its_literal_passage() -> None:
    propuesta = verificar_propuesta(
        TIQUE,
        [
            {
                "campo": "descripcion",
                "valor": "Lavadora carga frontal 8kg",
                "evidencia": "1 x LAVADORA CARGA FRONTAL 8KG",
            },
            {
                "campo": "establecimiento",
                "valor": "Electrodomésticos El Puente S.L.",
                "evidencia": "ELECTRODOMESTICOS EL PUENTE S.L.",
            },
            # Invented passage: not in the document.
            {"campo": "fecha_entrega", "valor": "2026-11-10", "evidencia": "Entrega: 10/11/2026"},
            # Real passage, but the value is not what it says.
            {"campo": "importe", "valor": "499.00", "evidencia": "TOTAL EUROS 449,00"},
        ],
    )
    assert propuesta.valor("descripcion") == "Lavadora carga frontal 8kg"
    assert propuesta.valor("establecimiento") == "Electrodomésticos El Puente S.L."
    assert propuesta.valor("fecha_entrega") is None
    assert propuesta.valor("importe") == "449.00"  # from the TOTAL line, not the wrong proposal
    assert set(propuesta.descartados) == {"fecha_entrega", "importe"}


def test_several_dates_are_not_guessed() -> None:
    propuesta = verificar_propuesta("Factura 01/10/2026\nAlbarán de entrega 05/10/2026\nTotal 10,00")
    assert propuesta.valor("fecha_compra") is None
    assert any("varias fechas" in a for a in propuesta.avisos)


class _Proveedor:
    def __init__(self, respuestas: list[dict[str, Any]]) -> None:
        self.respuestas = respuestas
        self.llamadas: list[dict[str, Any]] = []

    def complete_json(self, **kwargs: Any) -> dict[str, Any]:
        self.llamadas.append(kwargs)
        return self.respuestas[len(self.llamadas) - 1]


def test_text_file_is_read_once_and_checked_against_its_text() -> None:
    proveedor = _Proveedor(
        [
            {
                "campos": [
                    {
                        "campo": "descripcion",
                        "valor": "LAVADORA CARGA FRONTAL 8KG",
                        "evidencia": "LAVADORA CARGA FRONTAL 8KG",
                    }
                ]
            }
        ]
    )
    propuesta = leer_ticket(proveedor, filename="tique.txt", data=TIQUE.encode())
    assert len(proveedor.llamadas) == 1
    assert propuesta.valor("descripcion") == "LAVADORA CARGA FRONTAL 8KG"
    assert propuesta.valor("fecha_compra") == "2026-11-08"


def test_image_is_read_twice_and_only_agreeing_fields_survive() -> None:
    lectura = {
        "transcripcion": TIQUE,
        "campos": [
            {"campo": "fecha_compra", "valor": "2026-11-08", "evidencia": "Fecha: 08/11/2026"},
            {"campo": "importe", "valor": "449.00", "evidencia": "TOTAL EUROS 449,00"},
        ],
    }
    otra = {
        "transcripcion": TIQUE.replace("449,00", "443,00"),
        "campos": [
            {"campo": "fecha_compra", "valor": "2026-11-08", "evidencia": "Fecha: 08/11/2026"},
            {"campo": "importe", "valor": "443.00", "evidencia": "TOTAL EUROS 443,00"},
        ],
    }
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    propuesta = leer_ticket(_Proveedor([lectura, otra]), filename="foto.png", data=png)
    assert propuesta.valor("fecha_compra") == "2026-11-08"
    assert propuesta.valor("importe") is None
    assert all(c.origen == "imagen" for c in propuesta.campos)
    assert any("no coinciden" in a for a in propuesta.avisos)
