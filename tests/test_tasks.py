import tasks


class _DummyResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def test_enrich_caption_uses_correct_dvla_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        captured["timeout"] = timeout
        return _DummyResponse(
            200,
            {
                "make": "Ford",
                "colour": "Blue",
                "yearOfManufacture": 2019,
            },
        )

    monkeypatch.setattr(tasks, "load_known_plates", lambda: {})
    monkeypatch.setattr(tasks, "_audit_plate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tasks.requests, "post", fake_post)

    caption = tasks.enrich_caption_with_dvla(
        "Vehicle AB12 CDE arrived",
        {"name": "Driveway", "dvla_api_key": "test-key"},
        tag="[Driveway][abc12345]",
    )

    assert captured["url"] == "https://driver-vehicle-licensing.api.gov.uk/vehicle-enquiry/v1/vehicles"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["json"] == {"registrationNumber": "AB12CDE"}
    assert captured["timeout"] == 10
    assert "(Ford, Blue, 2019)" in caption
