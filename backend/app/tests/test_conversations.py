import uuid

from sqlalchemy import select

from app.database.models import Message


def test_requires_auth(client):
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/conversations", json={}).status_code == 401


def test_create_list_get_delete(client, make_user):
    _, h = make_user()
    c = client.post("/api/conversations", json={"title": "Plans"}, headers=h)
    assert c.status_code == 201
    cid = c.json()["id"]
    assert [x["id"] for x in client.get("/api/conversations", headers=h).json()] == [cid]
    assert client.get(f"/api/conversations/{cid}", headers=h).json()["messages"] == []
    assert client.delete(f"/api/conversations/{cid}", headers=h).status_code == 204
    assert client.get(f"/api/conversations/{cid}", headers=h).status_code == 404


def test_users_cannot_see_or_delete_each_others_conversations(client, make_user):
    _, ha = make_user("a@example.com", "sa")
    _, hb = make_user("b@example.com", "sb")
    cid = client.post("/api/conversations", json={}, headers=ha).json()["id"]
    assert client.get("/api/conversations", headers=hb).json() == []
    assert client.get(f"/api/conversations/{cid}", headers=hb).status_code == 404
    assert client.delete(f"/api/conversations/{cid}", headers=hb).status_code == 404
    assert client.get(f"/api/conversations/{cid}", headers=ha).status_code == 200


def test_unknown_id_404_and_title_validation(client, make_user):
    _, h = make_user()
    assert client.get(f"/api/conversations/{uuid.uuid4()}", headers=h).status_code == 404
    assert client.post("/api/conversations", json={"title": ""}, headers=h).status_code == 422


def test_deleting_conversation_cascades_messages(client, db_session, make_user):
    _, h = make_user()
    cid = client.post("/api/conversations", json={}, headers=h).json()["id"]
    db_session.add(Message(conversation_id=uuid.UUID(cid), role="user", content="hi"))
    db_session.commit()
    client.delete(f"/api/conversations/{cid}", headers=h)
    db_session.expire_all()
    assert db_session.scalar(select(Message)) is None
