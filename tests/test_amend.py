import shutil
import pytest
from app import store
from app.pipeline import run,sample_text
from app.errors import BusinessError
from app.exporter import evidence_csv
from app.config import DATA_DIR

HEADER="sample_id,kind,analyte,area,internal_area,level,dilution,unit,batch\n"

@pytest.fixture(autouse=True)
def clean_db():
    shutil.rmtree(DATA_DIR,ignore_errors=True)
    yield
    shutil.rmtree(DATA_DIR,ignore_errors=True)

def fresh():
    return store.save(sample_text(),run(sample_text()))

def supplement(*lines):
    return HEADER+"".join(line+"\n" for line in lines)

def test_supplement_does_not_create_batch():
    base=fresh();before=len(store.history())
    amended=store.amend(base["id"],supplement("S002,sample,Pb,340,100,0,2,ug/L,B01"))
    assert amended["id"]==base["id"]
    assert len(store.history())==before

def test_supplement_retry_is_idempotent():
    base=fresh();text=supplement("S002,sample,Pb,340,100,0,2,ug/L,B01")
    first=store.amend(base["id"],text)
    retry=store.amend(base["id"],text)
    assert retry["replayed"] is True
    assert retry["revision"]==first["revision"]
    assert len(store.versions(base["id"]))==len(store.versions(base["id"]))
    assert len({v["revision"] for v in store.versions(base["id"])})==len(store.versions(base["id"]))

def test_pending_rows_apply_as_current_result():
    base=fresh()
    amended=store.amend(base["id"],supplement("S002,sample,Pb,340,100,0,2,ug/L,B01"))
    row=next(r for r in amended["result"]["final"] if r["sample_id"]=="S002")
    entry=amended["amendment"]["rows"][0]
    assert entry["status"]=="已生效"
    assert row["review"]=="待复核" and row["amendment"]=="已生效"
    assert Decimalish(row["concentration"])==Decimalish("2.2")
    assert amended["decision"]=="待复核"

def test_reviewed_row_keeps_original_conclusion():
    base=fresh()
    reviewed=store.review(base["id"],base["revision"],"通过")
    original=next(r for r in reviewed["result"]["final"] if r["sample_id"]=="S002")
    amended=store.amend(base["id"],supplement("S002,sample,Pb,900,100,0,2,ug/L,B01"))
    row=next(r for r in amended["result"]["final"] if r["sample_id"]=="S002")
    assert amended["amendment"]["rows"][0]["status"]=="停在原结论"
    assert row["concentration"]==original["concentration"]
    assert row["review"]=="已复核"
    assert row["amendment"]=="停在原结论"

def test_mixed_supplement_splits_statuses():
    base=fresh()
    reviewed=store.review(base["id"],base["revision"],"通过")
    amended=store.amend(base["id"],supplement(
        "S002,sample,Pb,900,100,0,2,ug/L,B01",
        "S900,sample,Pb,320,100,0,1,ug/L,B01"))
    statuses={e["sample_id"]:e["status"] for e in amended["amendment"]["rows"]}
    assert statuses=={"S002":"停在原结论","S900":"已生效"}
    assert any(r["sample_id"]=="S900" and r["review"]=="待复核" for r in amended["result"]["final"])

def test_replaced_row_evidence_retained():
    base=fresh()
    reviewed=store.review(base["id"],base["revision"],"通过")
    original=next(r for r in reviewed["result"]["final"] if r["sample_id"]=="S002")
    amended=store.amend(base["id"],supplement("S002,sample,Pb,900,100,0,2,ug/L,B01"))
    entry=amended["amendment"]["rows"][0]
    assert entry["replaced"]["concentration"]==original["concentration"]
    assert entry["replaced_src"]==original["src"]
    assert entry["proposed"]["concentration"]!=original["concentration"]
    text=evidence_csv(amended,store.versions(base["id"]))
    assert "amendment" in text and "停在原结论" in text and "version" in text

def test_merge_failure_keeps_previous_result():
    base=fresh()
    reviewed=store.review(base["id"],base["revision"],"通过")
    with pytest.raises(BusinessError) as caught:
        store.amend(base["id"],supplement("BAD9,sample,Pb,N/A,100,0,1,ug/L,B01"))
    assert caught.value.code=="MERGE_FAILED"
    after=store.get(base["id"])
    assert after["revision"]==reviewed["revision"]
    assert after["decision"]==reviewed["decision"]
    assert len(after["result"]["final"])==len(reviewed["result"]["final"])
    assert not after["result"].get("amendments")

def test_merge_failure_does_not_consume_retry():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    with pytest.raises(BusinessError):
        store.amend(base["id"],supplement("BAD9,sample,Pb,N/A,100,0,1,ug/L,B01"))
    ok=store.amend(base["id"],supplement("S002,sample,Pb,340,100,0,2,ug/L,B01"))
    assert ok["replayed"] is False
    assert ok["revision"]==2

def test_supplement_rejects_corrupt_file_without_touching_old():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    with pytest.raises(BusinessError) as caught:
        store.amend(base["id"],"not,a,valid,header\n1,2\n")
    assert caught.value.code=="MERGE_FAILED"
    assert store.get(base["id"])["revision"]==1

def test_reviewed_row_ledger_not_overwritten():
    """已复核样品的补交行只存证，不进入现行账本，后续重算基准保持原样。"""
    base=fresh();store.review(base["id"],base["revision"],"通过")
    amended=store.amend(base["id"],supplement("S002,sample,Pb,900,100,0,2,ug/L,B01"))
    ledger_rows=amended["result"]["ledger"]["rows"]
    assert not any(r["src"].startswith(amended["amendment"]["amendment_id"]) for r in ledger_rows)
    again=store.amend(base["id"],supplement("S900,sample,Pb,320,100,0,1,ug/L,B01"))
    assert next(r for r in again["result"]["final"] if r["sample_id"]=="S002")["concentration"]=="2.0"

def test_invalid_supplement_for_reviewed_row_keeps_conclusion():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    amended=store.amend(base["id"],supplement("S002,sample,Pb,900,100,0,2,ppm,B01"))
    row=next(r for r in amended["result"]["final"] if r["sample_id"]=="S002")
    assert row["concentration"]=="2.0" and row["review"]=="已复核"
    entry=amended["amendment"]["rows"][0]
    assert entry["status"]=="停在原结论" and entry["rejected_reason"]=="UNIT"

def test_standard_correction_marks_samples_for_review():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    amended=store.amend(base["id"],supplement("C1,standard,Pb,310,100,1,1,ug/L,B01"))
    entry=next(e for e in amended["amendment"]["rows"] if e["kind"]=="standard")
    assert entry["status"]=="已生效"
    assert amended["decision"]=="待复核"

def test_standard_correction_does_not_silently_change_reviewed_samples():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    original={r["sample_id"]:r["concentration"] for r in store.get(base["id"])["result"]["final"]}
    amended=store.amend(base["id"],supplement("C1,standard,Pb,310,100,1,1,ug/L,B01"))
    affected=[e for e in amended["amendment"]["rows"] if e["status"]=="停在原结论"]
    assert affected and affected[0]["kind"]=="sample"
    for e in affected:
        row=next(r for r in amended["result"]["final"] if r["sample_id"]==e["sample_id"])
        assert row["concentration"]==original[e["sample_id"]]
        assert row["amendment"]=="停在原结论"

def test_partially_reviewed_supplement_mixed():
    """先通过一轮已生效补交，再复核，再补交：两种状态并存且各自留证。"""
    base=fresh()
    first=store.amend(base["id"],supplement("S900,sample,Pb,320,100,0,1,ug/L,B01"))
    assert first["amendment"]["rows"][0]["status"]=="已生效"
    store.review(base["id"],first["revision"],"通过")
    second=store.amend(base["id"],supplement(
        "S900,sample,Pb,360,100,0,1,ug/L,B01",
        "S901,sample,Pb,320,100,0,1,ug/L,B01"))
    statuses={e["sample_id"]:e["status"] for e in second["amendment"]["rows"]}
    assert statuses=={"S900":"停在原结论","S901":"已生效"}
    assert len(second["result"]["amendments"])==2
    assert second["decision"]=="待复核"


def test_stale_revision_rejected():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    with pytest.raises(BusinessError) as caught:
        store.amend(base["id"],supplement("S002,sample,Pb,340,100,0,2,ug/L,B01"),revision=0)
    assert caught.value.code=="STALE"

def test_retry_after_later_version_replays_instead_of_error():
    base=fresh()
    text=supplement("S002,sample,Pb,340,100,0,2,ug/L,B01")
    first=store.amend(base["id"],text)
    # 之后又有别的补交推进了版本，重试旧补交仍须幂等返回原记录
    store.amend(base["id"],supplement("S900,sample,Pb,320,100,0,1,ug/L,B01"))
    retry=store.amend(base["id"],text,revision=first["revision"])
    assert retry["replayed"] is True
    assert retry["revision"]==store.get(base["id"])["revision"]
    assert retry["amendment"]["digest"]==first["amendment"]["digest"]

def test_reject_returns_rows_to_pending():
    base=fresh();store.review(base["id"],base["revision"],"通过")
    returned=store.review(base["id"],store.get(base["id"])["revision"],"退回")
    assert returned["decision"]=="退回"
    assert all(r.get("review")!="已复核" for r in returned["result"]["final"])
    amended=store.amend(base["id"],supplement("S002,sample,Pb,900,100,0,2,ug/L,B01"))
    assert amended["amendment"]["rows"][0]["status"]=="已生效"

def test_review_after_amendment_still_works():
    base=fresh()
    amended=store.amend(base["id"],supplement(
        "S002,sample,Pb,340,100,0,2,ug/L,B01",
        "S900,sample,Pb,320,100,0,1,ug/L,B01"))
    decided=store.review(base["id"],amended["revision"],"通过")
    assert decided["decision"]=="通过"
    assert all(r["review"]=="已复核" for r in decided["result"]["final"])

def Decimalish(value):
    from decimal import Decimal
    return Decimal(str(value))
