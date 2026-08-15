"""YouTube Analytics metrics parsing + soft scope handling."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.services.youtube_analytics import (
    ANALYTICS_READONLY_SCOPE,
    CTR_UNAVAILABLE_NOTE,
    VIDEO_METRICS,
    VIDEO_METRICS_CORE,
    fetch_video_analytics,
    is_analytics_scope_error,
    is_unknown_metric_identifier_error,
    parse_analytics_report,
    token_has_analytics_scope,
)


def test_parse_analytics_report_ctr_avd_impressions():
    report = {
        "columnHeaders": [
            {"name": "video", "columnType": "DIMENSION"},
            {"name": "views", "columnType": "METRIC"},
            {"name": "estimatedMinutesWatched", "columnType": "METRIC"},
            {"name": "averageViewDuration", "columnType": "METRIC"},
            {"name": "averageViewPercentage", "columnType": "METRIC"},
            {"name": "impressions", "columnType": "METRIC"},
            {"name": "impressionClickThroughRate", "columnType": "METRIC"},
            {"name": "subscribersGained", "columnType": "METRIC"},
            {"name": "subscribersLost", "columnType": "METRIC"},
        ],
        "rows": [
            ["abc123", 1000, 250.5, 180.0, 42.5, 20000, 5.1, 12, 1],
        ],
    }
    out = parse_analytics_report(report, video_id="abc123")
    assert out["views"] == 1000
    assert out["impressions"] == 20000
    assert out["ctr_pct"] == 5.1
    assert out["avd_pct"] == 42.5
    assert out["average_view_duration_s"] == 180.0
    assert out["estimated_minutes_watched"] == 250.5
    assert out["subscribers_gained"] == 12
    assert out["source"] == "youtube_analytics"


def test_parse_avd_derived_from_duration():
    report = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "averageViewDuration"},
        ],
        "rows": [["vid", 120]],
    }
    out = parse_analytics_report(report, video_id="vid", duration_s=400)
    assert out["avd_pct"] == 30.0
    assert out.get("avd_derived") is True


def test_parse_ctr_ratio_scaled_when_low():
    report = {
        "columnHeaders": [
            {"name": "impressions"},
            {"name": "impressionClickThroughRate"},
        ],
        "rows": [[5000, 0.045]],
    }
    out = parse_analytics_report(report)
    assert out["ctr_pct"] == 4.5
    assert out.get("ctr_unit_note") == "scaled_from_ratio"


def test_token_has_analytics_scope():
    assert token_has_analytics_scope([ANALYTICS_READONLY_SCOPE]) is True
    assert token_has_analytics_scope(
        ["https://www.googleapis.com/auth/youtube.upload"]
    ) is False


def test_is_analytics_scope_error():
    assert is_analytics_scope_error(
        Exception("HttpError 403 insufficient authentication scopes")
    )
    assert is_analytics_scope_error(
        Exception("Access Not Configured. YouTube Analytics API has not been used")
    )
    assert not is_analytics_scope_error(Exception("quotaExceeded"))


def test_is_unknown_metric_identifier_error():
    assert is_unknown_metric_identifier_error(
        Exception('HttpError 400 ... "Unknown identifier (impressions)"')
    )
    assert is_unknown_metric_identifier_error(
        Exception("Unknown identifier (impressionClickThroughRate)")
    )
    assert not is_unknown_metric_identifier_error(Exception("quotaExceeded"))
    assert not is_unknown_metric_identifier_error(
        Exception("HttpError 403 insufficient authentication scopes")
    )


def test_fetch_video_analytics_mock_http():
    fake_report = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "impressions"},
            {"name": "impressionClickThroughRate"},
            {"name": "averageViewPercentage"},
        ],
        "rows": [["vid9", 50, 1000, 4.2, 38.0]],
    }
    mock_analytics = MagicMock()
    mock_analytics.reports.return_value.query.return_value.execute.return_value = (
        fake_report
    )
    with patch(
        "src.services.youtube_analytics.build_youtube_analytics_client",
        return_value=mock_analytics,
    ):
        out = fetch_video_analytics(
            credentials=object(),
            channel_id="UCtest",
            video_id="vid9",
            lookback_days=30,
        )
    assert out["ok"] is True
    assert out["ctr_pct"] == 4.2
    assert out["avd_pct"] == 38.0
    assert out["impressions"] == 1000
    mock_analytics.reports.return_value.query.assert_called_once()
    kwargs = mock_analytics.reports.return_value.query.call_args.kwargs
    assert kwargs["ids"] == "channel==UCtest"
    assert kwargs["filters"] == "video==vid9"
    assert kwargs["metrics"] == VIDEO_METRICS


def test_fetch_falls_back_to_core_when_impressions_unknown():
    fake_core = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "estimatedMinutesWatched"},
            {"name": "averageViewDuration"},
            {"name": "averageViewPercentage"},
            {"name": "subscribersGained"},
            {"name": "subscribersLost"},
        ],
        "rows": [["vid9", 50, 12.5, 180.0, 38.0, 2, 0]],
    }
    mock_analytics = MagicMock()
    execute = mock_analytics.reports.return_value.query.return_value.execute
    execute.side_effect = [
        Exception('HttpError 400 when requesting ... returned "Unknown identifier (impressions)"'),
        fake_core,
    ]
    with patch(
        "src.services.youtube_analytics.build_youtube_analytics_client",
        return_value=mock_analytics,
    ), patch(
        "src.services.youtube_reporting.fetch_video_reach_metrics",
        return_value={
            "ok": True,
            "impressions": None,
            "ctr_pct": None,
            "job_id": "job-1",
            "job_action": "existing",
            "reports_available": 0,
            "note": "Reporting API reach job exists but no daily report files yet",
        },
    ):
        out = fetch_video_analytics(
            credentials=object(),
            channel_id="UCtest",
            video_id="vid9",
            lookback_days=30,
        )
    assert out["ok"] is True
    assert out["views"] == 50
    assert out["avd_pct"] == 38.0
    assert out["average_view_duration_s"] == 180.0
    assert out["estimated_minutes_watched"] == 12.5
    assert out["ctr_pct"] is None
    assert out["impressions"] is None
    assert out.get("ctr_unavailable") is True
    assert "Unknown identifier" in out["note"]
    assert "Reporting API reach job" in out["note"]
    assert execute.call_count == 2
    metric_calls = [
        c.kwargs["metrics"]
        for c in mock_analytics.reports.return_value.query.call_args_list
    ]
    assert metric_calls[0] == VIDEO_METRICS
    assert metric_calls[1] == VIDEO_METRICS_CORE


def test_fetch_fills_ctr_from_reporting_reach_when_analytics_unknown():
    fake_core = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "averageViewPercentage"},
        ],
        "rows": [["vid9", 50, 38.0]],
    }
    mock_analytics = MagicMock()
    execute = mock_analytics.reports.return_value.query.return_value.execute
    execute.side_effect = [
        Exception('Unknown identifier (impressions)'),
        fake_core,
    ]
    with patch(
        "src.services.youtube_analytics.build_youtube_analytics_client",
        return_value=mock_analytics,
    ), patch(
        "src.services.youtube_reporting.fetch_video_reach_metrics",
        return_value={
            "ok": True,
            "impressions": 10000,
            "ctr_pct": 4.25,
            "job_id": "job-1",
            "reports_available": 3,
        },
    ):
        out = fetch_video_analytics(
            credentials=object(),
            channel_id="UCtest",
            video_id="vid9",
        )
    assert out["avd_pct"] == 38.0
    assert out["impressions"] == 10000
    assert out["ctr_pct"] == 4.25
    assert out.get("ctr_unavailable") is False
    assert out.get("ctr_source") == "youtube_reporting_reach"


def test_parse_audience_watch_ratio_at_elapsed():
    from src.services.youtube_analytics import parse_audience_watch_ratio_at_elapsed

    report = {
        "columnHeaders": [
            {"name": "elapsedVideoTimeRatio"},
            {"name": "audienceWatchRatio"},
        ],
        "rows": [
            [0.05, 0.92],
            [0.10, 0.81],
            [0.20, 0.70],
        ],
    }
    # 60s of a 600s video → target 0.10
    out = parse_audience_watch_ratio_at_elapsed(report, target_elapsed_ratio=0.10)
    assert out["first_60s_retention_pct"] == 81.0
    assert out["elapsed_video_time_ratio"] == 0.10


def test_parse_audience_watch_ratio_empty():
    from src.services.youtube_analytics import parse_audience_watch_ratio_at_elapsed

    out = parse_audience_watch_ratio_at_elapsed(
        {"columnHeaders": [{"name": "elapsedVideoTimeRatio"}, {"name": "audienceWatchRatio"}], "rows": []},
        target_elapsed_ratio=0.1,
    )
    assert out["first_60s_retention_pct"] is None
    assert out["note"] == "audience_retention_empty_rows"


def test_fetch_fills_first_60s_from_audience_retention():
    fake_core = {
        "columnHeaders": [
            {"name": "video"},
            {"name": "views"},
            {"name": "averageViewPercentage"},
        ],
        "rows": [["vid9", 50, 38.0]],
    }
    fake_ret = {
        "columnHeaders": [
            {"name": "elapsedVideoTimeRatio"},
            {"name": "audienceWatchRatio"},
        ],
        "rows": [[0.1, 0.75]],
    }
    mock_analytics = MagicMock()
    execute = mock_analytics.reports.return_value.query.return_value.execute
    execute.side_effect = [
        Exception("Unknown identifier (impressions)"),
        fake_core,
        fake_ret,
    ]
    with patch(
        "src.services.youtube_analytics.build_youtube_analytics_client",
        return_value=mock_analytics,
    ), patch(
        "src.services.youtube_reporting.fetch_video_reach_metrics",
        return_value={
            "ok": True,
            "impressions": None,
            "ctr_pct": None,
            "reports_available": 0,
            "note": "awaiting reports",
            "ctr_unavailable": True,
        },
    ):
        out = fetch_video_analytics(
            credentials=object(),
            channel_id="UCtest",
            video_id="vid9",
            duration_s=600,
        )
    assert out["avd_pct"] == 38.0
    assert out["first_60s_retention_pct"] == 75.0
    assert out.get("ctr_pending_reporting") is True
    assert out.get("first_60s_source") == "youtube_analytics_audience_retention"
