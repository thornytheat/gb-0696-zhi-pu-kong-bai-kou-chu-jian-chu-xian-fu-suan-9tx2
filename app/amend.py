import hashlib
import copy
from app.config import RULE_VERSION
from app.keys import row_key, key_text
from app.reader import read_rows
from app.errors import BusinessError
from app.report import plain
from app.modules.m1_intake.rules import FIELDS, ingest
from app.pipeline import run_ledger

APPLIED="已生效"          # 更正在现行结果中生效，样品回到待复核
HELD="停在原结论"         # 样品已复核，原结论保留，补交内容仅留证据
UNCHANGED="内容一致"      # 补交内容与现行结论一致，无实质变化
REJECTED="仍隔离"         # 补交行仍不合格，不能进入结果

def amendment_digest(text):
    return hashlib.sha256((RULE_VERSION+"\n"+text).encode("utf-8")).hexdigest()

def _prior_final(result):
    return {row_key(r): r for r in result.get("final",[])}

def _reviewed_keys(result):
    return {key for key,row in _prior_final(result).items() if row.get("review")=="已复核"}

def _ledger_of(result):
    """取得上一版现行行账；旧版本无行账时按既有结果尽力重建。"""
    ledger=result.get("ledger")
    if ledger is not None:
        return {"rows":list(ledger.get("rows",[])),"rejects":list(ledger.get("rejects",[])),
                "reader":list(ledger.get("reader",[])),"blank_rows":ledger.get("blank_rows",0)}
    balance=result.get("balance",{})
    rejects=[];reader=[]
    for q in result.get("quarantine",[]):
        raw=q.get("raw")
        if isinstance(raw,dict):
            rejects.append({"row":raw,"reason":q.get("reason","")})
        elif isinstance(raw,list):
            reader.append({"line":q.get("line",0),"src":q.get("src") or f"L{q.get('line',0)}",
                           "reason":q.get("reason",""),"raw":raw})
    return {"rows":list(result.get("raw",[])),"rejects":rejects,"reader":reader,
            "blank_rows":balance.get("blank",0)}

def _parse_supplement(text, aid):
    try:
        rows,bad,blank=read_rows(text,FIELDS)
    except BusinessError as exc:
        if exc.code=="SIZE": raise
        raise BusinessError("MERGE_FAILED",f"补交文件不可解析（{exc.code}），已保留旧版结果")
    for r in rows: r["src"]=f"{aid}::{r['src']}"
    for b in bad:
        b["src"]=f"{aid}::{b.get('src') or 'L'+str(b.get('line',0))}"
    ctx={"quarantine":[]}
    ingest(ctx,rows,reader_rejects=bad,blank_rows=blank)
    return ctx

def _same_result(a,b):
    if not a or not b: return False
    keys=("concentration","net","status","qualifier","lod","response")
    return all(str(a.get(k,""))==str(b.get(k,"")) for k in keys)

def _recompute(ledger):
    try:
        return run_ledger(ledger)
    except BusinessError as exc:
        raise BusinessError("MERGE_FAILED",f"合并复算失败（{exc.code}），已保留旧版结果")
    except Exception:
        raise BusinessError("MERGE_FAILED","合并复算失败，已保留旧版结果")

def _merge_ledger(base, rows, rejects, reader):
    keys={row_key(r) for r in rows}
    merged_rows=[r for r in base["rows"] if row_key(r) not in keys]+list(rows)
    merged_rejects=[e for e in base["rejects"] if row_key(e["row"]) not in keys]+[
        e for e in rejects if row_key(e["row"]) not in keys]
    return {"rows":merged_rows,"rejects":merged_rejects,"reader":list(base["reader"])+list(reader),
            "blank_rows":base["blank_rows"]}

def _freeze_row(row, old):
    """保留该层自身的字段结构，只把已复核样品原有的计算值覆盖回去。"""
    kept=dict(row)
    for key in kept:
        if key in old: kept[key]=old[key]
    kept["review"]="已复核"
    return kept

def _freeze_reviewed(result, prior, reviewed):
    """复算得到的下游数组里，已复核样品统一换回原结论，避免各层数字互相矛盾。"""
    # final 由 merge 单独处理（要保留本次更正标注），此处只冻结其上游各层
    for field in ("quality","concentrations","corrected","normalized"):
        rows=result.get(field)
        if not isinstance(rows,list): continue
        result[field]=[_freeze_row(r,prior[row_key(r)])
                       if isinstance(r,dict) and row_key(r) in reviewed and row_key(r) in prior else r
                       for r in rows]
    # 复核页与模块表格读的是各阶段 report，需要同步冻结后的行
    for rep in result.get("reports",[]):
        rows=rep.get("rows")
        if not isinstance(rows,list): continue
        rep["rows"]=[_freeze_row(r,prior[row_key(r)])
                     if isinstance(r,dict) and row_key(r) in reviewed and row_key(r) in prior else r
                     for r in rows]

def merge(current, text):
    """把补交行并入当前批次，返回 (新结果, 合并记录)。合并失败时抛出且不改动旧结果。"""
    digest=amendment_digest(text); aid=digest[:16]
    if not text.strip():
        raise BusinessError("MERGE_FAILED","补交内容为空，已保留旧版结果")
    sup=_parse_supplement(text,aid)
    sup_rows=sup["raw"]; sup_rejects=sup.get("intake_rejects",[])
    if not sup_rows:
        detail="；".join(sorted({e["reason"] for e in sup_rejects})) or "无有效数据行"
        raise BusinessError("MERGE_FAILED",f"补交没有可合并的样品行（{detail}），已保留旧版结果")

    base=_ledger_of(current)
    prior=_prior_final(current); reviewed=_reviewed_keys(current)
    sup_reader=list(sup["ledger"]["reader"])

    # 已复核样品的补交行不进入现行账本：其原结论（及其所依据的原始行）保持不变，
    # 仅在合并记录与导出中存证，避免标曲/空白重算基础被悄悄替换。
    open_rows=[r for r in sup_rows if row_key(r) not in reviewed]

    # 提案值：把全部补交行代入试算，用于说明已复核样品本会得到什么
    trial=_recompute(_merge_ledger(base,sup_rows,sup_rejects,sup_reader))
    proposed_by_key={row_key(r):r for r in trial.get("final",[])}
    normalized_src={r.get("src") for r in trial.get("normalized",[])}

    entries=[];counts={APPLIED:0,HELD:0,UNCHANGED:0,REJECTED:0}
    for r in sup_rows:
        key=row_key(r);old=prior.get(key);proposed=proposed_by_key.get(key)
        entry={"key":key_text(key),"batch":r.get("batch",""),"analyte":r.get("analyte",""),
               "sample_id":r.get("sample_id",""),"kind":r.get("kind",""),"line":r.get("line",0),
               "src":r.get("src",""),"amendment_id":aid,"digest":digest,
               "replaced":old,"replaced_src":old.get("src","") if old else "","submitted":r}
        if key in reviewed:
            same=old is not None and _same_result(old,proposed)
            if proposed is None:  # 补交行本身不合格：原结论保留，补交行仍隔离
                reason=next((q["reason"] for q in trial.get("quarantine",[]) if q.get("src")==r.get("src")),"QUARANTINED")
                entry.update(status=HELD,reason=f"样品已复核，原结论保留；补交行仍隔离（{reason}）",
                             current=old,proposed=None,rejected_reason=reason)
            else:
                entry.update(status=UNCHANGED if same else HELD,
                             reason="内容与原结论一致" if same else "样品已复核，原结论保留",
                             current=old,proposed=proposed)
        elif proposed is None and r.get("kind") in {"standard","blank"} and r.get("src") in normalized_src:
            # 标曲/空白不进样品结果，但其更正会改变现行复算依据
            entry.update(status=APPLIED,reason="标曲空白更正已生效，相关样品需复核",current=None,proposed=None)
        elif proposed is None:
            reason=next((q["reason"] for q in trial.get("quarantine",[]) if q.get("src")==r.get("src")),"QUARANTINED")
            entry.update(status=REJECTED,reason=reason,current=old,proposed=None)
        else:
            entry.update(status=APPLIED,reason="更正已生效，待复核",current=proposed,proposed=proposed)
        counts[entry["status"]]+=1
        entries.append(entry)

    # 同一份补交里未通过校验的行同样留痕，不能被静默丢弃
    accepted={row_key(r) for r in sup_rows}
    for e in sup_rejects:
        rr=e["row"]
        if row_key(rr) in accepted: continue
        entries.append({"key":key_text(row_key(rr)),"batch":rr.get("batch",""),"analyte":rr.get("analyte",""),
                        "sample_id":rr.get("sample_id",""),"kind":rr.get("kind",""),"line":rr.get("line",0),
                        "src":rr.get("src",""),"amendment_id":aid,"digest":digest,
                        "status":REJECTED,"reason":e["reason"],"submitted":rr,"replaced":prior.get(row_key(rr))})
        counts[REJECTED]+=1

    # 补交标曲/空白会间接改变其他已复核样品的计算值：同样标为停在原结论，不能静默保留旧值
    submitted={row_key(r) for r in sup_rows}|{row_key(e["row"]) for e in sup_rejects}
    for key in sorted(reviewed-submitted):
        old=prior.get(key);proposed=proposed_by_key.get(key)
        if old is None or proposed is None or _same_result(old,proposed): continue
        entries.append({"key":key_text(key),"batch":old.get("batch",""),"analyte":old.get("analyte",""),
                        "sample_id":old.get("sample_id",""),"kind":old.get("kind",""),
                        "line":old.get("line",0),"src":"","amendment_id":aid,"digest":digest,
                        "status":HELD,"reason":"受本次补交影响，原结论保留","current":old,
                        "proposed":proposed,"replaced":old,"replaced_src":old.get("src","")})
        counts[HELD]+=1

    if counts[APPLIED]==0 and counts[HELD]==0 and counts[UNCHANGED]==0:
        reasons=sorted({e["reason"] for e in entries})
        raise BusinessError("MERGE_FAILED",f"补交行均未形成有效结果（{'/'.join(reasons)}），已保留旧版结果")

    # 现行结果：仅并入未复核的补交行；已复核行沿用原结论，其补交行（含不合格者）不进入现行账本
    open_rejects=[e for e in sup_rejects if row_key(e["row"]) not in reviewed]
    computed=_recompute(_merge_ledger(base,open_rows,open_rejects,sup_reader)) if open_rows else current
    by_key={e["key"]:e for e in entries}
    records=[];seen=set()
    for r in computed.get("final",[]):
        key=row_key(r);hit=by_key.get(key_text(key));old=prior.get(key)
        if key in reviewed and old is not None:
            kept=dict(old);kept["review"]="已复核"
            if hit: kept["amendment"]=hit["status"]
        else:
            kept=dict(r);kept["review"]="待复核"
            if hit and hit["status"]==APPLIED: kept["amendment"]=APPLIED
        records.append(kept);seen.add(key)
    for key,old in prior.items():  # 已复核行本轮未进入复算时仍保留原结论
        if key in reviewed and key not in seen:
            kept=dict(old);kept["review"]="已复核"
            hit=by_key.get(key_text(key))
            if hit: kept["amendment"]=hit["status"]
            records.append(kept);seen.add(key)
    result=copy.deepcopy(computed)
    result["final"]=plain(records)
    result["blocked"]=(not records) or bool(result.get("curve_errors")) or any(
        r.get("status") in {"OVER_RANGE","CONTROL_FAIL"} for r in records)
    result["rule_version"]=RULE_VERSION
    _freeze_reviewed(result,prior,reviewed)

    record=plain({"amendment_id":aid,"digest":digest,"rule_version":RULE_VERSION,
                  "rows":entries,"counts":counts,
                  "effective":counts[APPLIED],
                  "supplement":{"input":sup.get("input_count",0),"blank":sup.get("blank_rows",0),
                                "quarantined":len(sup.get("quarantine",[]))}})
    result["amendments"]=list(current.get("amendments",[]))+[record]
    return result,record
