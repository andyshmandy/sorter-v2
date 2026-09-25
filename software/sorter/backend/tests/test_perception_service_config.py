from perception import service


def test_resolve_algorithm_id_per_channel_uses_scope_defaults(monkeypatch):
    defaults = {
        "feeder": "bundled:feeder-default",
        "carousel": "bundled:carousel-default",
    }

    monkeypatch.setattr(
        service,
        "normalize_detection_algorithm",
        lambda scope, value: value if value else defaults[scope],
    )

    resolved = service._resolve_algorithm_id_per_channel(None, None)

    assert resolved == {
        2: "bundled:feeder-default",
        3: "bundled:feeder-default",
        4: "bundled:carousel-default",
    }


def test_resolve_algorithm_id_per_channel_keeps_role_and_scope_boundaries(monkeypatch):
    defaults = {
        "feeder": "bundled:feeder-default",
        "carousel": "bundled:carousel-default",
    }

    def _normalize(scope: str, value: str | None) -> str:
        if value in (None, "", "unsupported"):
            return defaults[scope]
        return value

    monkeypatch.setattr(service, "normalize_detection_algorithm", _normalize)

    resolved = service._resolve_algorithm_id_per_channel(
        {
            "algorithm": "bundled:shared-feeder",
            "algorithm_by_role": {
                "c_channel_2": "bundled:c2-specific",
                "c_channel_3": "unsupported",
            },
        },
        {"algorithm": "unsupported"},
    )

    assert resolved == {
        2: "bundled:c2-specific",
        3: "bundled:feeder-default",
        4: "bundled:carousel-default",
    }
