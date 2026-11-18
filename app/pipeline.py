from pathlib import Path
from app.report import plain,reason_counts
from app.modules.m1_intake.rules import apply as stage1, apply_ledger
from app.modules.m2_internal.rules import apply as stage2
from app.modules.m3_curve.rules import apply as stage3
from app.modules.m4_blank.rules import apply as stage4
from app.modules.m5_concentration.rules import apply as stage5
from app.modules.m6_quality.rules import apply as stage6
from app.modules.m7_review.rules import apply as stage7

STAGES=(stage2,stage3,stage4,stage5,stage6,stage7)

def sample_text():
    return (Path(__file__).parent.parent/"sample_data"/"instrument.csv").read_text(encoding="utf-8-sig")

def _stages(ctx):
    for stage in STAGES:
        ctx["reports"].append(stage(ctx))

def run(text):
    ctx={"text":text,"quarantine":[],"reports":[]}
    ctx["reports"].append(stage1(ctx))
    _stages(ctx)
    ctx.pop("text",None)
    ctx["reasons"]=reason_counts(ctx)
    return plain(ctx)

def run_ledger(ledger):
    """以已合并的现行行账重算，供补交合并在同一批次内生成新版结果。"""
    ctx={"quarantine":[],"reports":[]}
    ctx["reports"].append(apply_ledger(ctx,ledger))
    _stages(ctx)
    ctx["reasons"]=reason_counts(ctx)
    return plain(ctx)
