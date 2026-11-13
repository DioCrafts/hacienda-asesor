def test_the_module_exposes_the_document_hook_the_core_uses() -> None:
    from hacienda_gpt.api.routers.modulos import _herramienta_de_documento
    from hacienda_gpt.modulos.caducidades import MODULO

    assert callable(getattr(MODULO, "leer_documento", None))
    assert _herramienta_de_documento(MODULO) == "caducidades_registrar_compra"
