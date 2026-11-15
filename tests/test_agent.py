import asyncio
import pytest

from terra.agent import (heuristic_mode, select_acquisitions, verify_numbers, context_near,
                        investigation_scope, measured_answer, write_answer, condition_bullets)

AOI = [-74.8, -5.55, -74.5, -5.25]


def obs(i, date, track=147, direction="ASCENDING", frame=175, bbox=(-75.5, -6.5, -74.0, -4.5), pols=("HH", "HV")):
    return {"id": f"NISAR_{i}", "product": "GCOV", "date": date, "track": track, "direction": direction, "frame": frame,
            "bbox": list(bbox), "polarizations": list(pols), "download_url": f"https://nisar.asf.earthdatacloud.nasa.gov/{i}.h5", "maturity": "VALIDATED"}


def test_change_pairs_same_geometry_with_widest_span():
    items = [obs(1, "2026-03-20T10:00:00Z"), obs(2, "2026-06-12T10:00:00Z"), obs(3, "2026-10-22T10:00:00Z"),
             obs(4, "2026-10-21T23:00:00Z", track=155, direction="DESCENDING", frame=91)]
    ids, why = select_acquisitions("change", items, AOI)
    assert ids == ["NISAR_1", "NISAR_3"]
    assert "216 days" in why


def test_change_recent_pair_preference():
    items = [obs(1, "2026-03-20T10:00:00Z"), obs(2, "2026-06-12T10:00:00Z"), obs(3, "2026-10-22T10:00:00Z")]
    ids, _ = select_acquisitions("change", items, AOI, "recent_pair")
    assert ids == ["NISAR_2", "NISAR_3"]


def test_change_without_matching_geometry_is_refused():
    items = [obs(1, "2026-03-20T10:00:00Z"), obs(2, "2026-06-12T10:00:00Z", track=155)]
    ids, why = select_acquisitions("change", items, AOI)
    assert ids == [] and "track" in why


def test_backscatter_picks_latest_covering_product():
    items = [obs(1, "2026-03-20T10:00:00Z"), obs(2, "2026-10-22T10:00:00Z"), obs(3, "2026-11-01T10:00:00Z", bbox=(-80, 10, -79, 11))]
    ids, _ = select_acquisitions("backscatter", items, AOI)
    assert ids == ["NISAR_2"]


def test_numbers_are_traced_with_display_rounding():
    evidence = {"median_db": -5.651581, "valid_projected_area_km2": 1102.0696, "date": "2026-10-22"}
    ok = verify_numbers("Median backscatter was −5.65 dB over 1,102 km² on 2026-10-22.", evidence)
    assert ok["status"] == "all_traced" and ok["numbers_checked"] >= 3
    bad = verify_numbers("Median backscatter was −7.9 dB.", evidence)
    assert bad["unmatched"] == ["−7.9"]


def test_heuristic_mode():
    assert heuristic_mode("Has flooding increased since March?") == "change"
    assert heuristic_mode("What does the radar see here?") == "backscatter"


def test_change_honours_named_dates_and_matching_processing():
    items = [obs(1, "2026-03-20T10:00:00Z"), obs(2, "2026-06-12T10:00:00Z"), obs(3, "2026-10-22T10:00:00Z"),
             {**obs(5, "2026-10-10T10:00:00Z"), "maturity": "PROVISIONAL"}]
    ids, _ = select_acquisitions("change", items, AOI, "widest_span", "2026-06-15", "2026-10-15")
    assert ids == ["NISAR_2", "NISAR_3"]


def test_wrong_sign_and_unrelated_small_numbers_are_rejected():
    assert verify_numbers("Backscatter increased by 5.65 dB.", {"median_db": -5.651})["status"] == "untraced_numbers"
    assert verify_numbers("There were 2 observations.", {"observation_count": 1})["status"] == "untraced_numbers"
    assert verify_numbers("The median was 1004 dB.", {"median_db": 1000})["status"] == "untraced_numbers"


@pytest.mark.asyncio
async def test_context_providers_start_concurrently_and_keep_source_status(monkeypatch):
    from terra import feeds
    ready = asyncio.Event()
    started = []
    def loader(name):
        async def load():
            started.append(name)
            if len(started) == 4:
                ready.set()
            await asyncio.wait_for(ready.wait(), 0.5)
            return {"items": [], "source": name, "status": "stale" if name == "fires" else "live", "fetched_at": "2026-01-01T00:00:00Z"}
        return load
    for name in ("earthquakes", "events", "fires", "nisar_recent"):
        monkeypatch.setattr(feeds, name, loader(name))
    result = await context_near(AOI, timeout=1)
    assert result["unavailable"] == []
    assert result["sources"]["fires"]["status"] == "stale"
    assert result["sources"]["earthquakes"]["fetched_at"]


def test_distinct_event_types_get_scientific_capability_boundaries():
    for question, topic in [("Has the ground subsided?", "deformation"), ("Is the glacier moving?", "glacier_motion"),
                            ("Did this forest burn?", "fire_and_vegetation"), ("Was the river flooded?", "flood_and_water"),
                            ("Is the coast eroding?", "coastal_change"), ("Was there a hurricane?", "weather")]:
        result = investigation_scope(question)
        assert result["topic"] == topic
        assert result["event_classification"] == "not_performed"
        assert result["additional_evidence_needed"]


def test_immediate_computed_answer_has_traceable_measurements():
    investigation = {"question": "Did flooding increase?", "result": {
        "measurements": {"observations": [{"valid_pixels": 120, "median_db": -5.651, "coverage_pct": 93.4}]},
        "validation": {"limitations": ["Single acquisition"]},
        "provenance": {"inputs": [{"metadata": {"date": "2026-01-01", "track": 147}, "catalog": {}}]},
    }}
    result = measured_answer(investigation)
    assert result["verification"]["status"] == "all_traced"
    assert "temporal change is not established" in result["headline"]
    assert "does not classify flood extent" in result["answer"]


@pytest.mark.asyncio
async def test_model_cannot_use_numbers_from_user_question_as_evidence(monkeypatch):
    from terra import agent
    async def fake_chat(*args, **kwargs):
        return {"headline": "The difference is 99 dB.", "answer": "", "evidence": [], "possible_explanations": [], "caveats": [], "next_steps": []}, {"model": "test"}
    monkeypatch.setattr(agent, "_chat", fake_chat)
    investigation = {"question": "Was the difference 99 dB?", "result": {"measurements": {"change": {"median_db": -5.65}}, "validation": {}, "provenance": {}}}
    with pytest.raises(ValueError, match="could not be traced"):
        await write_answer(investigation, {})


def test_dual_polarization_pairs_are_preferred_so_the_change_can_be_split():
    """Etna, track 51: 2026-06-17 is HH-only, 08-28 .. 10-15 carry HH+HV. The widest pair (06-17 -> 10-15) could not say
    which polarization shows the change; the widest dual-polarization pair (08-28 -> 10-15) can, and the plan says so."""
    items = [obs(1, "2026-06-17T17:55:06Z", pols=("HH",)), obs(2, "2026-08-28T17:55:02Z"), obs(3, "2026-09-21T17:55:02Z"), obs(4, "2026-10-15T17:55:02Z")]
    ids, why = select_acquisitions("change", items, AOI)
    assert ids == ["NISAR_2", "NISAR_4"]
    assert "Both dates carry HH and HV" in why and "2026-06-17" in why and "48 days" in why
    assert select_acquisitions("change", items, AOI, "recent_pair")[0] == ["NISAR_3", "NISAR_4"]
    # Named dates are honoured even when that costs the cross-polarized channel, and the plan says what was lost.
    ids, why = select_acquisitions("change", items, AOI, "widest_span", "2026-06-15", "2026-10-15")
    assert ids == ["NISAR_1", "NISAR_4"] and "cannot be split by polarization" in why
    # One dual-polarization date is not a pair: fall back to the widest span.
    ids, _ = select_acquisitions("change", [obs(1, "2026-06-17T17:55:06Z", pols=("HH",)), obs(4, "2026-10-15T17:55:02Z")], AOI)
    assert ids == ["NISAR_1", "NISAR_4"]


def test_acquisitions_with_different_polarizations_are_not_compared():
    items = [obs(1, "2026-03-20T10:00:00Z", pols=("HH",)), obs(2, "2026-06-12T10:00:00Z", pols=("VV",))]
    assert select_acquisitions("change", items, AOI)[0] == []


def test_model_context_values_and_dates_are_evidence_but_source_prose_is_not():
    from terra.agent import _evidence, _verification_evidence
    conditions = {"source": "Model release 9999", "acquisitions": [{"date": "2026-10-01T10:00:00Z", "model_time": "2026-10-01T10:00:00Z",
                  "rain_72h_mm": 12.4, "rain_observed_hours": 72, "rain_expected_hours": 72,
                  "soil_moisture_m3m3": 0.173, "soil_layer": "0–7 cm"}]}
    evidence = _verification_evidence(_evidence({"result": {"surface_conditions": conditions}}, {}))
    assert verify_numbers("Modelled soil moisture was 0.173 on October 1, 2026, with 12.4 mm of rain.", evidence)["status"] == "all_traced"
    assert verify_numbers("Modelled rain was 9999 mm.", evidence)["status"] == "untraced_numbers"
    assert verify_numbers("Modelled rain was 12.4 mm on October 2, 2026.", evidence)["status"] == "untraced_numbers"


def test_incomplete_rainfall_is_not_reported_as_a_complete_total():
    incomplete = {"acquisitions": [{"date": "2026-10-01", "rain_72h_mm": 0, "rain_observed_hours": 65,
                                    "rain_expected_hours": 72, "soil_moisture_m3m3": None}]}
    assert condition_bullets(incomplete) == []
    complete = {"acquisitions": [{"date": "2026-10-01", "rain_72h_mm": 0, "rain_observed_hours": 72,
                                  "rain_expected_hours": 72, "soil_moisture_m3m3": None}]}
    assert "0.0 mm" in condition_bullets(complete)[0]


@pytest.mark.asyncio
async def test_narration_gets_radar_date_conditions_concurrently_with_context(monkeypatch, tmp_path):
    import json
    from terra import activities, agent, conditions
    dates = ["2026-03-20T10:00:00Z", "2026-10-22T10:00:00Z"]
    result = {"measurements": {"observations": [{"valid_pixels": 120, "median_db": -5.65, "coverage_pct": 95.0}]},
              "validation": {"limitations": ["Model context does not establish causes."]},
              "provenance": {"inputs": [{"metadata": {"date": date}, "catalog": {}} for date in dates]}}
    (tmp_path / "result.json").write_text(json.dumps(result))
    gate = asyncio.Event()
    started = []
    async def live_context(bbox):
        started.append("context")
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 0.5)
        return {"events": [], "unavailable": []}
    async def weather(bbox, requested_dates):
        assert requested_dates == dates
        assert bbox == AOI
        started.append("conditions")
        if len(started) == 2:
            gate.set()
        await asyncio.wait_for(gate.wait(), 0.5)
        return {"status": "available", "source": "test model", "acquisitions": [
            {"date": date, "model_time": date, "model": "ERA5", "soil_layer": "0–7 cm", "rain_72h_mm": 12.5,
             "rain_observed_hours": 72, "rain_expected_hours": 72, "soil_moisture_m3m3": 0.173} for date in dates], "unavailable": []}
    monkeypatch.setattr(activities, "safe_job_directory", lambda _id: tmp_path)
    monkeypatch.setattr(agent, "context_near", live_context)
    monkeypatch.setattr(agent, "available", lambda: False)
    monkeypatch.setattr(conditions, "surface_conditions", weather)
    response = await activities.narrate_findings({"id": "test", "bbox": AOI, "question": "Did flooding change?"})
    assert response["surface_conditions"]["requested_dates"] == dates
    assert response["surface_conditions"]["comparison"]["rain_72h_change_mm"] == 0.0
    assert response["narrative"]["verification"]["status"] == "all_traced"
    assert any("Modelled conditions on 2026-03-20" in line for line in response["narrative"]["evidence"])


@pytest.mark.asyncio
async def test_surface_condition_timeout_is_unknown_and_not_zero(monkeypatch, tmp_path):
    from terra import activities, conditions
    async def weather(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(conditions, "surface_conditions", weather)
    monkeypatch.setattr(activities, "SURFACE_CONDITIONS_TIMEOUT", 0.01)
    result = {"provenance": {"inputs": [{"metadata": {"date": "2026-10-01T10:00:00Z"}}]}}
    response = await activities._surface_conditions({"bbox": AOI}, result, tmp_path)
    assert response["status"] == "unavailable"
    assert response["acquisitions"] == []
    assert response["unavailable"] == ["2026-10-01T10:00:00Z"]


@pytest.mark.asyncio
async def test_different_soil_layers_and_models_are_not_subtracted(monkeypatch, tmp_path):
    from terra import activities, conditions
    dates = ["2026-03-20T10:00:00Z", "2026-10-22T10:00:00Z"]
    async def weather(*args):
        return {"status": "available", "acquisitions": [
            {"date": dates[0], "model": "ERA5", "soil_layer": "0–7 cm", "soil_moisture_m3m3": 0.15},
            {"date": dates[1], "model": "forecast", "soil_layer": "1–3 cm", "soil_moisture_m3m3": 0.22}], "unavailable": []}
    monkeypatch.setattr(conditions, "surface_conditions", weather)
    response = await activities._surface_conditions({"bbox": AOI}, {"provenance": {"inputs": [{"metadata": {"date": d}} for d in dates]}}, tmp_path)
    assert "soil_moisture_change_m3m3" not in response["comparison"]
    assert response["comparison"]["soil_moisture_comparability"].startswith("not comparable")


@pytest.mark.asyncio
async def test_publisher_keeps_weather_in_result_and_downloadable_report(monkeypatch, tmp_path):
    import json
    from terra import activities
    result = {"measurements": {"observations": [{"valid_pixels": 120, "median_db": -5.65, "coverage_pct": 95.0}]},
              "validation": {"limitations": []}, "provenance": {"inputs": []}, "report": "Original computed report.\n", "layers": []}
    (tmp_path / "result.json").write_text(json.dumps(result))
    monkeypatch.setattr(activities, "safe_job_directory", lambda _id: tmp_path)
    weather = {"status": "available", "source": "Open-Meteo ERA5", "acquisitions": [{"date": "2026-10-01T10:00:00Z",
               "model_time": "2026-10-01T10:00:00Z", "model": "ERA5", "source_url": "https://open-meteo.com/",
               "rain_72h_mm": 12.5, "rain_observed_hours": 72, "rain_expected_hours": 72, "soil_moisture_m3m3": 0.173, "soil_layer": "0–7 cm"}], "unavailable": []}
    response = await activities.publish_science({"id": "test", "bbox": AOI, "surface_conditions": weather, "question": "What changed?"})
    assert response["surface_conditions"] == weather
    text = (tmp_path / "report.md").read_text()
    assert "12.5 mm" in text and "0.173 m³/m³" in text
    assert "not NISAR measurements or proof" in text
    assert "Original computed report." in text
    assert response["report"] == text


def test_hotspot_magnitude_is_explicitly_derived_without_ignoring_signs_globally():
    from terra.agent import _evidence, _verification_evidence
    investigation = {"question": "Where did the surface change?", "result": {"measurements": {
        "observations": [], "change": {"median_db": -2.3, "valid_pixels": 120, "threshold_exceedance_pct": 45.0,
        "hotspots": [{"direction": "decrease", "mean_change_db": -5.51, "area_km2": 0.24, "centre": [-5.4, -74.6]}]}},
        "validation": {"limitations": []}, "provenance": {"inputs": []}}}
    evidence = _evidence(investigation, {})
    assert evidence["derived"]["hotspot_change_magnitudes_db"] == [5.51]
    assert verify_numbers("Darker by 5.5 dB", _verification_evidence(evidence))["status"] == "all_traced"
    assert measured_answer(investigation)["verification"]["status"] == "all_traced"
    assert verify_numbers("The median increased by 2.3 dB", {"median_db": -2.3})["status"] == "untraced_numbers"


def test_dates_are_checked_as_dates():
    evidence = {"inputs": [{"date": "2026-10-25T01:13:29Z"}, {"date": "2026-11-06T01:13:20Z"}], "median_db": 0.98}
    ok = verify_numbers("Backscatter rose 0.98 dB between October 25 and 6 November 2026 (2026-11-06).", evidence)
    assert ok["unmatched"] == [] and ok["numbers_checked"] == 4
    bad = verify_numbers("Between October 24 and November 6.", evidence)
    assert bad["unmatched"] == ["October 24"]


@pytest.mark.asyncio
async def test_writer_retries_once_naming_untraced_values_and_can_cite_net_change(monkeypatch):
    from terra import agent
    calls = []
    async def fake_chat(system, user, *args, **kwargs):
        calls.append(user)
        empty = {"evidence": [], "possible_explanations": [], "caveats": [], "next_steps": []}
        if len(calls) == 1:
            return {"headline": "Water-like area fell by 16.95 km².", "answer": "", **empty}, {"model": "test"}
        return {"headline": "Water-like area fell by 16.9 km², from 39.23 to 22.33 km².", "answer": "", **empty}, {"model": "test"}
    monkeypatch.setattr(agent, "_chat", fake_chat)
    investigation = {"question": "Has flooding changed?", "result": {"measurements": {"change": {
        "median_db": 1.57, "low_return_before_km2": 39.2292, "low_return_after_km2": 22.3312}}, "validation": {}, "provenance": {}}}
    answer = await write_answer(investigation, {})
    assert len(calls) == 2 and "16.95" in calls[1] and "rewrite_instruction" in calls[1]
    assert answer["verification"]["status"] == "all_traced"
