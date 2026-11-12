"""``params-verify --fichero`` accepts repeated or comma-separated names and refuses unknown ones."""

from __future__ import annotations

import click
import pytest

from hacienda_gpt.cli.proactive import _ficheros_pedidos


def test_repeated_and_comma_separated_names_are_the_same_request() -> None:
    assert _ficheros_pedidos(("sociedades,isd", " Patrimonio ")) == ("sociedades", "isd", "patrimonio")
    assert _ficheros_pedidos(()) == ()


def test_an_unknown_name_is_an_error_not_an_empty_report() -> None:
    with pytest.raises(click.BadParameter, match="sucesiones"):
        _ficheros_pedidos(("sucesiones",))
