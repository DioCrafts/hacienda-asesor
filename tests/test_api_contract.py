from fastapi.testclient import TestClient

from hacienda_gpt.api.api import app


client = TestClient(app)


def test_health() -> None:
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json()['status'] == 'ok'


def test_create_case_and_get_case_and_audit() -> None:
    created = client.post('/cases', json={'user_id': 'u1', 'jurisdiction': 'ES', 'tax_period': '2025'})
    assert created.status_code == 200
    case_id = created.json()['case_id']

    got = client.get(f'/cases/{case_id}')
    assert got.status_code == 200
    assert got.json()['case_id'] == case_id

    audit = client.get(f'/cases/{case_id}/audit')
    assert audit.status_code == 200
    assert audit.json()['case_id'] == case_id


def test_post_turn_contract() -> None:
    created = client.post('/cases', json={'user_id': 'u2', 'jurisdiction': 'ES', 'tax_period': '2025'})
    case_id = created.json()['case_id']
    turn = client.post(f'/cases/{case_id}/turn', json={'user_input': 'Soy residente en España y tengo dudas de IRPF 2025'})
    assert turn.status_code == 200
    body = turn.json()
    assert body['case_id'] == case_id
    assert 'facts' in body
    assert 'missing_facts' in body
    assert 'candidate_obligation_ids' in body
    assert 'next_questions' in body


def test_post_turn_updates_case_tax_period_from_extracted_fact() -> None:
    created = client.post('/cases', json={'user_id': 'u3', 'jurisdiction': 'ES', 'tax_period': '2025'})
    case_id = created.json()['case_id']

    turn = client.post(
        f'/cases/{case_id}/turn',
        json={'user_input': 'Tengo dudas de IRPF para 2024 y soy residente en España'},
    )
    assert turn.status_code == 200

    got = client.get(f'/cases/{case_id}')
    assert got.status_code == 200
    assert got.json()['tax_period'] == '2024'


def test_not_found_case() -> None:
    r = client.get('/cases/does-not-exist')
    assert r.status_code == 404
