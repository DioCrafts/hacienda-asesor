"""Prompts and response schemas of the re-encoding loop.

The model never decides what is written to a parameter file: it PROPOSES a
value and quotes, and the deterministic gate (``control``) decides. The prompts
therefore say what the gate will check, so that an honest reading passes it.
"""

from __future__ import annotations

import inspect
from typing import Any

from pydantic import BaseModel

REGLAS_COMUNES = """\
Rules that are never relaxed (a program checks most of them after you answer and rejects what breaks them):
1. NOTHING FROM MEMORY. Every figure and every condition comes from the legal text between <texto_legal> tags. If the \
text does not say it, you do not encode it. What you remember about this law is irrelevant, even if you are sure.
2. QUOTES ARE VERBATIM. Each item of `evidencia` is ONE contiguous passage copied character by character from the \
text (no "[...]", no stitching, no paraphrase, no corrections). Whitespace does not matter; everything else does.
3. EVERY NUMBER INSIDE A QUOTE. Each figure you encode — amounts, percentages, ceilings, ages, days, inhabitants, \
factors, wherever it is nested — must be printed in some item of `evidencia`, in the law's own notation.
4. NO DERIVED FIGURES. Never encode a number you computed (a cap that is "percentage × base", a sum of two figures, a \
rounded value). Encode what the law prints, with the field meant for it, or leave it out.
5. CONSERVATIVE. A wrong figure is far worse than a missing one. If the wording does not fit the schema EXACTLY, do \
not approximate: answer "no_calculable" and say precisely which datum or formula is missing.
6. THE WORDING SHOWN IS THE ONLY ONE YOU HAVE. The text is the consolidated wording published by the BOE, followed by \
its annotations ("Se modifica, con efectos desde…, por…", "Redacción mostrada por el BOE: … en vigor a partir del…"). \
Wordings announced for the future, or superseded, are not to be encoded. When the task says the law is NOT YET \
CONSOLIDATED, the text is instead the amending norm itself: encode the new wording it gives between quotation \
marks. When a bracketed line says only one apartado of a long article is shown, the rest of the article exists and \
you may ask for another apartado in `articulos_necesarios` ("5.uno").
7. EFFECTS DATE. State from which date the wording you encoded produces effects (`efectos_desde`, AAAA-MM-DD) and \
quote in `cita_efectos` the literal passage that says so. An effects clause ("con efectos desde el 1 de enero de \
2025", "para los períodos impositivos iniciados a partir de…") always wins over the entry into force: laws are \
often published in one year with effects from 1 January of that year or of the previous one. Only when no clause \
states the effects, quote the "Redacción mostrada por el BOE" line and give its "en vigor a partir del" date. If the \
law limits the wording to a period, give `efectos_hasta` as well and quote it; otherwise leave it empty. The tax \
years the change touches are computed from these dates by a program — do not guess them.
8. User-facing strings (summaries, titles, unchecked requirements, reasons) are written in Spanish and contain only \
figures that are inside your quotes, written as the law prints them.
"""

SISTEMA_LECTOR = """\
You maintain the legal parameters of a Spanish tax calculator. The calculator is deterministic: it reads DATA. Your \
job is to read one group of parameters from the current text of its law and propose that data, with the literal \
quotes that prove it. A program validates your proposal and an independent reviewer then tries to refute it with \
boundary cases; what does not survive is not used.

{reglas}
What you are encoding: {que}

{guia}

Schema of the value you return in `valor_json` (a JSON object serialised as a string; decimal figures as strings \
with a dot, fractions for percentages unless the schema says otherwise):

{esquema}

How to answer:
- `resultado`: "codificada" when the text fits the schema exactly; "no_calculable" when it does not (then `motivo` \
says exactly why and `valor_json` is ""); "sin_cambios" ONLY when the task carries <revision_tras_modificacion> \
and the text shown leaves the previous encoding exactly as it is (see there).
- `articulos_necesarios`: if the text shown refers to rules that live in another article of the same law and you \
need them (common income ceilings, definitions, incompatibilities), list those articles here ("13", "2 bis") using \
the index in <indice_de_la_ley> and return `resultado` "no_calculable" with `motivo` "faltan artículos": you will be \
asked again with them. Otherwise leave it empty.
- `cambios`: one or two sentences in Spanish for the maintainer saying what differs from the previous encoding.
- When <objeciones> is present, a previous proposal of yours was rejected. Each objection is either a rule you broke \
or a case where the law gives one amount and your encoding another. Fix the encoding; do not argue. If an objection \
shows the schema cannot express the law, answer "no_calculable".
"""

SISTEMA_REVISOR = """\
You are the independent reviewer of a proposed encoding of Spanish tax law for a deterministic calculator. You are \
given the legal text and the proposal — nothing else. You did not write it and you owe it nothing: your job is to \
find where it departs from the law. Read the legal text FIRST and decide what it says; only then compare.

{reglas_revisor}
What was encoded: {que}

{guia}

Schema the proposal follows:

{esquema}

What you return:
- `discrepancias`: every concrete departure from the law. `gravedad` "error" when some taxpayer would get a different \
amount or a different yes/no than the law gives (an omitted condition the schema could check, a wrong unit or base, \
a threshold that is inclusive in the law and exclusive in the encoding or the reverse, an approximated or derived \
figure, a wording with other effects); "observacion" for anything that changes no result (style of a summary, a \
requirement that could be worded better). `cita`: the literal passage of the law that shows it. Do not invent \
discrepancies to look diligent: an empty list is a valid answer.
- `conforme`: true only when you found no "error".
- `efectos_desde` and `cita_efectos`: YOUR OWN reading of the date from which this wording produces effects, with \
the literal passage (rule: an effects clause wins over the entry into force; without one, the "Redacción mostrada \
por el BOE" line). You are not shown the encoder's answer on purpose.
- `casos`: boundary test cases, at least {min_casos} and at most 8, chosen to break the encoding: exactly at each \
threshold, one unit below and above it, each variant or tier, the cap being reached, a taxpayer who does NOT \
qualify. For each one give the amount THE LAW gives (computed by you from the legal text, never from the proposal) \
and in `fundamento` the literal passage that supports it. A program runs each case through the calculator with \
the proposed data and compares.
{guia_casos}
"""

REGLAS_REVISOR = """\
Rules:
1. Judge only against the text between <texto_legal> tags (and its annotations). Nothing from memory.
2. Quotes you give (`cita`, `fundamento`, `cita_efectos`) are verbatim, one contiguous passage each.
3. Things the calculator cannot check are acceptable ONLY if the proposal says so openly to the user (for regional \
deductions, in `requisitos_no_comprobados`). A silently dropped condition is an error.
4. An encoding may give LESS than the law in an edge case if it says so; it must never give MORE.
"""

ESQUEMA_LECTOR: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "resultado": {"type": "string", "enum": ["codificada", "no_calculable", "sin_cambios"]},
        "valor_json": {"type": "string"},
        "evidencia": {"type": "array", "items": {"type": "string"}},
        "efectos_desde": {"type": "string"},
        "efectos_hasta": {"type": "string"},
        "cita_efectos": {"type": "string"},
        "articulos_necesarios": {"type": "array", "items": {"type": "string"}},
        "motivo": {"type": "string"},
        "cambios": {"type": "string"},
    },
    "required": [
        "resultado",
        "valor_json",
        "evidencia",
        "efectos_desde",
        "efectos_hasta",
        "cita_efectos",
        "articulos_necesarios",
        "motivo",
        "cambios",
    ],
}


REVISION_TRAS_MODIFICACION = """\
This article was amended ({motivo}). The previous encoding (<codificacion_anterior>) may still be exactly right, or \
the amendment may have changed it, possibly OUTSIDE the passages it quoted: a new condition, exception, limit, \
figure or effects clause anywhere in the article. Read the WHOLE text shown, not only the old quotes.
- If nothing in the current text changes this group, answer `resultado` "sin_cambios" with `valor_json` "", and in \
`evidencia` copy verbatim the passages that print each figure of the previous encoding (the old quotes, when they \
are still there, count already). A program checks that every old quote is still in the text and every figure is \
inside a quote; an independent reviewer then looks for whatever you may have missed.
- If anything changes it, encode the group as usual ("codificada" or "no_calculable").
"""

REVISION_SIN_CAMBIOS = """\
The encoder says that this article, although amended, does NOT change the encoding under <propuesta>: it is the one \
in force today, not a new proposal. Prove it wrong if you can. Look for any condition, exception, limit, figure, \
income ceiling, period or effects clause in the text that the encoding does not reflect, or reflects differently; \
each one is an "error". An empty list of discrepancies means you looked and found none. `efectos_desde` and \
`cita_efectos`: the date from which the wording shown produces effects, as usual. The boundary cases are run with \
this encoding.
"""


def esquema_revisor(extra: dict[str, Any]) -> dict[str, Any]:
    """Response schema of the reviewer; ``extra`` are the adapter's own independent readings."""
    propiedades: dict[str, Any] = {
        "conforme": {"type": "boolean"},
        "discrepancias": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "gravedad": {"type": "string", "enum": ["error", "observacion"]},
                    "tipo": {
                        "type": "string",
                        "enum": [
                            "condicion_omitida",
                            "unidad_o_base",
                            "umbral",
                            "aproximacion",
                            "cifra",
                            "efectos",
                            "otro",
                        ],
                    },
                    "descripcion": {"type": "string"},
                    "cita": {"type": "string"},
                },
                "required": ["gravedad", "tipo", "descripcion", "cita"],
            },
        },
        "efectos_desde": {"type": "string"},
        "cita_efectos": {"type": "string"},
        "casos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "descripcion": {"type": "string"},
                    "datos_json": {"type": "string"},
                    "importe_esperado": {"type": "string"},
                    "fundamento": {"type": "string"},
                },
                "required": ["descripcion", "datos_json", "importe_esperado", "fundamento"],
            },
        },
        **extra,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": propiedades,
        "required": list(propiedades),
    }


SISTEMA_DESGLOSE = """\
You maintain the legal parameters of a Spanish tax calculator. You are given ONE article of a law (or the passage \
of an amending norm that rewrites it) that may hold several independent items of this kind: {que}

List every such item the text establishes — nothing else is asked of you now: do not encode amounts or conditions.

Rules (a program checks them and drops what breaks them):
1. Only what the text between <texto_legal> tags establishes. Nothing from memory.
2. `etiqueta`: where the item is in the law, starting EXACTLY with «{etiqueta}» and followed by its apartado as the \
law numbers it, joined by dots («{etiqueta}.Dos», «{etiqueta}.3», «{etiqueta}.Uno.a)»). When the article holds a single \
item, the label is «{etiqueta}» itself. When one apartado holds two items with different amounts and conditions (a \
general one and one for a special group), list it once: the encoder handles variants.
3. `titulo`: the heading the law gives the item, in Spanish, or a short description when it has none.
4. `cita`: ONE contiguous passage copied verbatim from the text, taken from that item (its heading or first sentence).
5. Skip apartados that set common rules, definitions, limits or procedures and establish no item of their own, and \
apartados printed as suppressed, repealed or annulled.
"""

ESQUEMA_DESGLOSE: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "elementos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "etiqueta": {"type": "string"},
                    "titulo": {"type": "string"},
                    "cita": {"type": "string"},
                },
                "required": ["etiqueta", "titulo", "cita"],
            },
        }
    },
    "required": ["elementos"],
}


SISTEMA_PRECEPTOS = """\
You are given passages of a Spanish AMENDING norm and the name of ONE law it may amend: {ley}. List every article or \
provision of THAT law which the norm modifies, adds, replaces, suppresses or repeals. Nothing else is asked of you: \
do not encode amounts or conditions, and do not list articles of other laws, nor articles of the amending norm itself.

Rules (a program checks them and drops what breaks them):
1. Only what the text between <texto_de_la_norma> tags says. Nothing from memory.
2. `articulo`: the article as the amended law numbers it ("5", "9 bis", "68"), or a provision as "disposición \
adicional 3" / "disposición transitoria 18" / "disposición final 2" (number in figures).
3. `cita`: ONE contiguous passage copied verbatim from the text, of at least a few words, that NAMES that article or \
provision as being modified, added or suppressed (the sentence "Se modifica el artículo 5…", "Uno. El apartado 2 del \
artículo 9 queda redactado…").
4. If the text does not say which articles of that law it changes, return an empty list.
"""

ESQUEMA_PRECEPTOS: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "preceptos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {"articulo": {"type": "string"}, "cita": {"type": "string"}},
                "required": ["articulo", "cita"],
            },
        }
    },
    "required": ["preceptos"],
}


def fuente_de(
    modelos: list[type[BaseModel]], *, modulo: Any, desde: str | None = None, hasta: str | None = None
) -> str:
    """The schema as the code states it: the source of the pydantic models, comments and validators included.

    Taken from the running code so it cannot drift from what the gate validates. ``desde``/``hasta`` cut a slice of
    the module (constants with the allowed vocabularies live next to the models); when the source is not available
    the JSON Schema of the models is used instead.
    """
    try:
        if desde is not None:
            completo = inspect.getsource(modulo)
            inicio = completo.index(desde)
            fin = completo.index(hasta, inicio) if hasta else len(completo)
            return completo[inicio:fin].rstrip()
        return "\n\n".join(inspect.getsource(m).rstrip() for m in modelos)
    except (OSError, TypeError, ValueError):
        import json

        return "\n\n".join(json.dumps(m.model_json_schema(), ensure_ascii=False) for m in modelos)


def etiquetar(nombre: str, contenido: str, **atributos: Any) -> str:
    attrs = "".join(f' {k}="{v}"' for k, v in atributos.items() if v not in (None, ""))
    return f"<{nombre}{attrs}>\n{contenido}\n</{nombre}>"
