from app.reader import read_rows
from app.report import report,quarantine
FIELDS="sample_id kind analyte area internal_area level dilution unit batch".split()

def ingest(ctx, rows, reader_rejects=(), intake_rejects=(), blank_rows=0, rejects_in_rows=True):
    """校验并采信已解析行；reader_rejects 为文件结构级拒绝，intake_rejects 为既有拒绝记录。"""
    ctx["quarantine"].extend(reader_rejects)
    kept=[];seen={};rejected=[]
    for r in rows:
        if not r.get("sample_id"): rejected.append({"row":r,"reason":"ID_REQUIRED"});continue
        key=(r["batch"],r["analyte"],r["sample_id"])
        content=tuple(r[k] for k in FIELDS)
        if key in seen:
            rejected.append({"row":r,"reason":"DUPLICATE" if seen[key]==content else "KEY_CONFLICT"});continue
        seen[key]=content;kept.append(r)
    rejects=list(intake_rejects)+rejected
    ctx["raw"]=kept
    for e in rejects: quarantine(ctx,e["row"],e["reason"])
    ctx["blank_rows"]=blank_rows
    # 输入行数按物理行计：文件路径下行内的业务拒绝已包含在 rows 中
    ctx["input_count"]=len(rows)+(0 if rejects_in_rows else len(intake_rejects))+len(reader_rejects)+blank_rows
    ctx["ledger"]={"rows":kept,"rejects":rejects,"reader":list(reader_rejects),"blank_rows":blank_rows}
    return report(kept,采信=len(kept),隔离=len(ctx["quarantine"]),空行=blank_rows)

def apply(ctx):
    rows,bad,blank=read_rows(ctx["text"],FIELDS)
    return ingest(ctx,rows,reader_rejects=bad,blank_rows=blank)

def apply_ledger(ctx,ledger):
    return ingest(ctx,ledger.get("rows",[]),ledger.get("reader",[]),ledger.get("rejects",[]),
                  ledger.get("blank_rows",0),rejects_in_rows=False)
