"""Cohort continuity, configuration evidence, hash separation, and the fresh
initialization lifecycle of the instrument ledger."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mnemoseed_local.instrument.ledger import (
    LEDGER_DDL,
    CohortIdentityMismatchError,
    FreshInit,
    InstrumentError,
    InstrumentPaths,
    LedgerMissingError,
    WriterLock,
    WriterLockHeld,
    build_config_evidence,
    canonical_json,
    check_cohort_continuity,
    compute_config_hash,
    compute_instrument_hash,
    compute_source_hash,
    create_ledger_schema,
    list_cohorts,
    open_ledger,
    read_manifest,
    schema_hash,
    sha256_hex,
    verify_config_evidence,
)

APPROVED_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "cohort_id",
        "kind",
        "seed_manifest_hash",
        "schema_hash",
        "instrument_hash",
        "config_hash",
        "control_id",
        "cohort_created_at_raw",
        "frozen_at_iso",
    }
)


def _write_manifest(paths: InstrumentPaths, manifest: dict[str, Any]) -> None:
    paths.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    paths.manifest_path.write_text(canonical_json(manifest), encoding="utf-8")


def test_the_manifest_holds_exactly_the_approved_keys(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    home = initialize_home()
    manifest = read_manifest(paths)
    assert set(manifest) == APPROVED_MANIFEST_KEYS
    assert manifest["manifest_version"] == 1
    assert manifest["cohort_id"] == home.primary.cohort_id
    assert manifest["kind"] == home.primary.kind
    assert manifest["seed_manifest_hash"] == home.primary.seed_manifest_hash
    assert manifest["schema_hash"] == home.primary.schema_hash
    assert manifest["instrument_hash"] == home.primary.instrument_hash
    assert manifest["config_hash"] == home.primary.config_hash
    assert manifest["control_id"] == home.primary.control_id
    assert manifest["cohort_created_at_raw"] == home.primary.cohort_created_at_raw
    assert isinstance(manifest["frozen_at_iso"], str)


def test_continuity_accepts_the_frozen_home(paths: InstrumentPaths, initialize_home) -> None:
    home = initialize_home()
    assert check_cohort_continuity(paths, home.ledger) == home.primary


def test_continuity_fails_when_the_manifest_is_missing(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    home = initialize_home()
    paths.manifest_path.unlink()
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"
    assert not paths.manifest_path.exists()
    assert len(list_cohorts(home.ledger)) == 2


def test_continuity_fails_on_a_wrong_manifest_version(
    paths: InstrumentPaths,
    initialize_home,
    manifest_builder,
) -> None:
    home = initialize_home()
    _write_manifest(paths, manifest_builder(home.primary, manifest_version=2))
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"


def test_continuity_fails_on_an_unknown_manifest_key(
    paths: InstrumentPaths,
    initialize_home,
    manifest_builder,
) -> None:
    home = initialize_home()
    _write_manifest(paths, manifest_builder(home.primary, extra_key="value"))
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"


def test_continuity_fails_when_the_cohort_row_is_missing(
    paths: InstrumentPaths,
    initialize_home,
    manifest_builder,
) -> None:
    home = initialize_home()
    _write_manifest(paths, manifest_builder(home.primary, cohort_id="0" * 64))
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"
    assert len(list_cohorts(home.ledger)) == 2


def test_continuity_fails_when_the_row_identity_differs(
    paths: InstrumentPaths,
    initialize_home,
    manifest_builder,
) -> None:
    home = initialize_home()
    _write_manifest(paths, manifest_builder(home.primary, cohort_created_at_raw=float.hex(1.7e9)))
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"


def test_continuity_fails_when_a_hash_differs(
    paths: InstrumentPaths,
    initialize_home,
    manifest_builder,
) -> None:
    home = initialize_home()
    _write_manifest(paths, manifest_builder(home.primary, config_hash="0" * 64))
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"


def test_continuity_fails_when_the_linked_control_row_is_missing(
    paths: InstrumentPaths,
    initialize_home,
) -> None:
    home = initialize_home(with_control=False)
    assert home.control is None
    with pytest.raises(LedgerMissingError) as failure:
        check_cohort_continuity(paths, home.ledger)
    assert failure.value.diag_code == "ledger_missing"


def test_continuity_fails_on_an_empty_ledger(
    paths: InstrumentPaths,
    cohort_builder,
    manifest_builder,
) -> None:
    create_ledger_schema(paths)
    ledger = open_ledger(paths)
    try:
        cohort = cohort_builder()
        _write_manifest(paths, manifest_builder(cohort))
        with pytest.raises(LedgerMissingError) as failure:
            check_cohort_continuity(paths, ledger)
        assert failure.value.diag_code == "ledger_missing"
        assert list_cohorts(ledger) == ()
    finally:
        ledger.close()


def test_the_omitted_denominator_defaults_to_the_frozen_integer(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    assert parsed["denominator"] == 30
    assert isinstance(parsed["denominator"], int)
    assert not isinstance(parsed["denominator"], bool)
    text = build_config_evidence(
        home=str(paths.home.resolve()),
        kind=cohort.kind,
        cohort_id=cohort.cohort_id,
        control_id=cohort.control_id,
        repair_limit=None,
    )
    assert json.loads(text)["denominator"] == 30
    assert not isinstance(json.loads(text)["denominator"], bool)


def test_the_config_evidence_is_canonical_and_binds_the_home(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    assert set(parsed) == {"home", "denominator", "kind", "cohort_id", "control_id", "repair_limit"}
    assert canonical_json(parsed) == cohort.config_json
    assert parsed["home"] == str(paths.home.resolve())
    assert parsed["denominator"] == 30
    assert parsed["kind"] == cohort.kind
    assert parsed["cohort_id"] == cohort.cohort_id
    assert parsed["control_id"] == cohort.control_id
    assert parsed["repair_limit"] is None
    assert cohort.config_hash == compute_config_hash(cohort.config_json)
    assert cohort.config_hash == sha256_hex(cohort.config_json.encode("utf-8"))


def test_verify_config_evidence_accepts_the_frozen_text(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    verify_config_evidence(cohort, paths=paths)


def test_verify_config_evidence_rejects_a_wrong_hash(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(replace(cohort, config_hash="0" * 64), paths=paths)


def test_verify_config_evidence_rejects_a_non_canonical_text(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    text = json.dumps(parsed, sort_keys=True)
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_verify_config_evidence_rejects_a_foreign_home(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    parsed["home"] = str((paths.home.parent / "elsewhere").resolve())
    text = canonical_json(parsed)
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_verify_config_evidence_rejects_a_wrong_denominator(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    parsed["denominator"] = 29
    text = canonical_json(parsed)
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_verify_config_evidence_rejects_an_unlisted_repair_limit(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    parsed = json.loads(cohort.config_json)
    parsed["repair_limit"] = 50
    text = canonical_json(parsed)
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_a_bool_denominator_never_yields_accepted_evidence(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    try:
        text = build_config_evidence(
            home=str(paths.home.resolve()),
            kind=cohort.kind,
            cohort_id=cohort.cohort_id,
            control_id=cohort.control_id,
            repair_limit=None,
            denominator=True,
        )
    except InstrumentError:
        return
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_a_bool_repair_limit_never_yields_accepted_evidence(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    cohort = cohort_builder()
    try:
        text = build_config_evidence(
            home=str(paths.home.resolve()),
            kind=cohort.kind,
            cohort_id=cohort.cohort_id,
            control_id=cohort.control_id,
            repair_limit=True,
        )
    except InstrumentError:
        return
    tampered = replace(cohort, config_json=text, config_hash=compute_config_hash(text))
    with pytest.raises(CohortIdentityMismatchError):
        verify_config_evidence(tampered, paths=paths)


def test_the_schema_hash_digests_the_frozen_ddl() -> None:
    assert schema_hash() == sha256_hex(LEDGER_DDL.encode("utf-8"))


def test_the_four_hashes_are_distinct(
    paths: InstrumentPaths,
    cohort_builder,
    source_manifest,
    source_commit,
) -> None:
    cohort = cohort_builder()
    instrument = compute_instrument_hash()[0]
    source = compute_source_hash(source_commit=source_commit, source_manifest=source_manifest)[0]
    assert len({cohort.schema_hash, instrument, source, cohort.config_hash}) == 4


def test_the_source_hash_is_a_declared_digest(
    source_manifest,
    source_commit,
) -> None:
    digest, files = compute_source_hash(source_commit=source_commit, source_manifest=source_manifest)
    expected = sha256_hex(
        canonical_json({"commit": source_commit, "files": source_manifest}).encode("utf-8"),
    )
    assert digest == expected
    assert len(files) == len(source_manifest)


def _refuse_file_reads(*args: object, **kwargs: object) -> bytes:
    raise AssertionError("the declared source manifest must not read file bytes")


def test_the_source_hash_does_not_verify_the_declared_files(
    monkeypatch: pytest.MonkeyPatch,
    source_manifest,
    source_commit,
) -> None:
    declared = {path: sha256_hex(f"declared:{path}".encode()) for path in source_manifest}
    expected = sha256_hex(
        canonical_json({"commit": source_commit, "files": declared}).encode("utf-8"),
    )
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", _refuse_file_reads)
        patch.setattr(Path, "read_text", _refuse_file_reads)
        digest, files = compute_source_hash(source_commit=source_commit, source_manifest=declared)
    assert digest == expected
    assert files == [{"path": path, "sha256": declared[path]} for path in sorted(declared)]
    other = {**declared, "storage/ports.py": sha256_hex(b"another declared digest")}
    assert compute_source_hash(source_commit=source_commit, source_manifest=other)[0] != digest


def test_the_source_manifest_must_match_the_frozen_path_set(
    source_manifest,
    source_commit,
) -> None:
    extended = {**source_manifest, "instrument/ledger.py": "0" * 64}
    with pytest.raises(CohortIdentityMismatchError) as failure:
        compute_source_hash(source_commit=source_commit, source_manifest=extended)
    assert failure.value.diag_code in {"cohort_identity_mismatch", "store_binding_mismatch"}


def test_the_source_manifest_must_cover_every_frozen_path(
    source_manifest,
    source_commit,
) -> None:
    partial = {key: value for key, value in source_manifest.items() if key != "storage/ports.py"}
    with pytest.raises(CohortIdentityMismatchError):
        compute_source_hash(source_commit=source_commit, source_manifest=partial)


@pytest.mark.parametrize(
    "digest",
    ["not-a-digest", "A" * 64, "0" * 63, "0" * 65],
    ids=["not-hex", "upper-case", "too-short", "too-long"],
)
def test_the_source_manifest_digests_must_have_the_frozen_shape(
    source_manifest,
    source_commit,
    digest: str,
) -> None:
    broken = {**source_manifest, "storage/ports.py": digest}
    with pytest.raises(CohortIdentityMismatchError):
        compute_source_hash(source_commit=source_commit, source_manifest=broken)


def test_the_source_manifest_paths_must_be_relative(
    source_manifest,
    source_commit,
) -> None:
    escaping = {**source_manifest, "storage/../storage/ports.py": "0" * 64}
    with pytest.raises(CohortIdentityMismatchError):
        compute_source_hash(source_commit=source_commit, source_manifest=escaping)


def test_fresh_init_holds_the_writer_lock_across_the_whole_sequence(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    with FreshInit(paths) as context:
        with pytest.raises(WriterLockHeld):
            WriterLock(paths.writer_lock).acquire()
        context.assert_fresh()
    lock = WriterLock(paths.writer_lock)
    lock.acquire()
    lock.release()


def test_fresh_init_checks_freshness_before_seeding(paths: InstrumentPaths) -> None:
    create_ledger_schema(paths)
    calls: list[str] = []
    with pytest.raises(CohortIdentityMismatchError) as failure:
        with FreshInit(paths) as context:
            context.seed(lambda _paths: calls.append("seeded"))
    assert "partial_initialization" in str(failure.value.detail)
    assert calls == []


def test_fresh_init_refuses_a_non_empty_invocations_directory(paths: InstrumentPaths) -> None:
    paths.invocations_dir.mkdir(parents=True, exist_ok=True)
    (paths.invocations_dir / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(CohortIdentityMismatchError) as failure:
        with FreshInit(paths):
            pass
    assert "partial_initialization" in str(failure.value.detail)


def test_fresh_init_accepts_an_absent_or_empty_invocations_directory(
    paths: InstrumentPaths,
) -> None:
    with FreshInit(paths) as context:
        context.assert_fresh()
    paths.invocations_dir.mkdir(parents=True, exist_ok=True)
    with FreshInit(paths) as context:
        context.assert_fresh()


def test_fresh_init_ignores_the_lock_file_alone(paths: InstrumentPaths) -> None:
    paths.writer_lock.parent.mkdir(parents=True, exist_ok=True)
    paths.writer_lock.write_text("999999", encoding="utf-8")
    with FreshInit(paths) as context:
        context.assert_fresh()


def test_fresh_init_refuses_partial_leftovers_without_reseeding(paths: InstrumentPaths) -> None:
    paths.cohort_graph_db.parent.mkdir(parents=True, exist_ok=True)
    paths.cohort_graph_db.write_bytes(b"partial")
    calls: list[str] = []
    with pytest.raises(CohortIdentityMismatchError) as failure:
        with FreshInit(paths) as context:
            context.seed(lambda _paths: calls.append("seeded"))
    assert "partial_initialization" in str(failure.value.detail)
    assert calls == []
    assert paths.cohort_graph_db.read_bytes() == b"partial"


def test_fresh_init_does_not_mistake_its_own_product_databases_for_leftovers(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    def seed(instrument_paths: InstrumentPaths) -> None:
        instrument_paths.cohort_graph_db.parent.mkdir(parents=True, exist_ok=True)
        instrument_paths.cohort_graph_db.write_bytes(b"seeded")

    with FreshInit(paths) as context:
        context.seed(seed)
        ledger = context.create_ledger()
        context.register(cohort_builder())
        assert paths.cohort_graph_db.exists()
        assert ledger is not None


def test_fresh_init_writes_the_manifest_last(
    paths: InstrumentPaths,
    cohort_builder,
) -> None:
    with FreshInit(paths) as context:
        context.seed(lambda _paths: None)
        context.create_ledger()
        assert paths.ledger_db.exists()
        assert not paths.manifest_path.exists()
        registered = context.register(cohort_builder())
        assert not paths.manifest_path.exists()
        context.write_manifest(registered)
        assert paths.manifest_path.exists()


def test_fresh_init_refuses_to_seed_outside_the_context(paths: InstrumentPaths) -> None:
    calls: list[str] = []
    context = FreshInit(paths)
    with pytest.raises(CohortIdentityMismatchError) as failure:
        context.seed(lambda _paths: calls.append("seeded"))
    assert "freshness_not_verified" in str(failure.value.detail)
    assert calls == []
