import copy


MACRO_VIEW = {
    "metadata": {
        "speaker_name": "Test Analyst",
        "speaker_role": "Macro Strategist",
        "speaker_institution": "Test: Research",
        "source_channel": "Test_Channel",
        "broadcast_date": "2026-08-07",
        "video_id": "abcdefghijk",
    },
    "graph_nodes": {
        "time_box": "[[2026-H2]]",
        "macro_themes": ["[[Inflation]]"],
        "asset_classes": ["[[Bonds]]"],
        "specific_tickers": ["[[TLT]]"],
    },
    "quant_signals": {
        "bull_bear_score": 3,
        "conviction_score": 8,
        "contrarian_flag": True,
        "sector_tilt": "[[Defensives]]",
        "duration_call": "Long",
        "macro_factor": "Inflation",
        "view_time_horizon": "Months",
    },
    "view_details": {
        "core_thesis": 'Rates fall after the "peak".',
        "conditional_catalysts": ["CPI cools"],
        "invalidation_risks": ["Inflation reaccelerates"],
        "verbatim_quote": 'The "peak" is behind us.',
        "key_data_points": [
            {"indicator": "CPI", "value": "2.5", "unit": "%", "context": "YoY"}
        ],
        "additional_quotes": ["Duration should rally."],
        "price_targets": [
            {"ticker": "[[TLT]]", "direction": "up", "target": "110", "horizon": "Months"}
        ],
    },
    "expectation_gap": "Market prices sticky inflation.",
    "causal_chain": ["CPI down", "Yields down", "Bonds up"],
    "tracking_indicators": [
        {"metric": "CPI", "threshold": "below 2.8%", "implication": "Yields fall"},
        {"metric": "10Y yield", "threshold": "below 4%", "implication": "Bonds rally"},
    ],
    "tactical_stance": [
        {"asset": "Bonds", "stance": "overweight", "reason": "Disinflation"}
    ],
}


def macro_view(**metadata_overrides):
    data = copy.deepcopy(MACRO_VIEW)
    data["metadata"].update(metadata_overrides)
    return data
