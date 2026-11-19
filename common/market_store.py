#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全市场日 K 本地库：2024-01-01 起，按交易日分片，支持增量更新与严格离线读取。

持久化格式为 state/market_daily/YYYY/YYYY-MM-DD.csv.gz，每行一只股票。
历史 code+date 一旦写入便不被后续前复权结果覆盖，避免除权因子变化导致
每天重写全部历史。common/state/klines/ 只是供旧模型使用的可重建物化缓存。
"""
import argparse
import csv
import glob
import gzip
import io
import json
import math
import os
import tempfile
from collections import deque
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
START_DATE = "2024-01-01"
STORE_DIR = os.path.join(BASE_DIR, "state", "market_daily")
FIELDS = ("code", "date", "open", "high", "low", "close", "volume", "turnover")
BAR_FIELDS = FIELDS[1:]


class MarketDataError(RuntimeError):
    """本地行情缺失或损坏；离线调用方应显式处理，不能改为访问外网。"""


def _code(value):
    code = str(value or "").strip()
    if code.startswith(("sh", "sz", "bj")):
        code = code[2:]
    code = code.zfill(6)
    if len(code) != 6 or not code.isdigit() or code == "000000":
        raise ValueError("无效股票代码: %r" % value)
    return code


def _date(value):
    text = str(value or "")[:10]
    datetime.strptime(text, "%Y-%m-%d")
    return text


def _float(value, field):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        raise ValueError("%s 不是数值: %r" % (field, value))
    if not math.isfinite(number):
        raise ValueError("%s 不是有限数值" % field)
    return number


def _bar(value, code=None):
    date = _date(value.get("date") or value.get("day"))
    result = {
        "code": _code(code or value.get("code")),
        "date": date,
        "open": _float(value.get("open"), "open"),
        "high": _float(value.get("high"), "high"),
        "low": _float(value.get("low"), "low"),
        "close": _float(value.get("close", value.get("last")), "close"),
        "volume": _float(value.get("volume", value.get("vol")), "volume"),
        "turnover": _float(value.get("turnover", value.get("exchange")), "turnover"),
    }
    if min(result[k] for k in ("open", "high", "low", "close")) <= 0:
        raise ValueError("价格必须大于 0")
    if result["volume"] < 0:
        raise ValueError("成交量不能为负数")
    return result


def _atomic_bytes(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _gzip_csv(rows):
    """生成确定性 gzip；相同内容不会因 gzip 时间戳产生无意义 diff。"""
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row[k] for k in FIELDS})
    out = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as fh:
        fh.write(text.getvalue().encode("utf-8"))
    return out.getvalue()


class MarketStore:
    def __init__(self, root=STORE_DIR):
        self.root = os.path.abspath(root)
        self.manifest_path = os.path.join(self.root, "manifest.json")

    def shard_path(self, date):
        date = _date(date)
        return os.path.join(self.root, date[:4], date + ".csv.gz")

    def _dates(self, start=None, end=None):
        start = _date(start or START_DATE)
        end = _date(end) if end else None
        dates = []
        filename = ("%s-%s-%s.csv.gz" %
                    ("[0-9]" * 4, "[0-9]" * 2, "[0-9]" * 2))
        for path in glob.glob(os.path.join(self.root, "[0-9]" * 4, filename)):
            date = os.path.basename(path)[:10]
            if date >= start and (end is None or date <= end):
                dates.append(date)
        return sorted(set(dates))

    def _read_shard(self, date):
        path = self.shard_path(date)
        if not os.path.exists(path):
            return {}
        rows = {}
        try:
            with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
                for raw in csv.DictReader(fh):
                    bar = _bar(raw)
                    rows[bar["code"]] = bar
        except Exception as exc:
            raise MarketDataError("行情分片损坏 %s: %s" % (path, exc)) from exc
        return rows

    def _manifest(self):
        try:
            with open(self.manifest_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        data.setdefault("format_version", 1)
        data.setdefault("start_date", START_DATE)
        data.setdefault("codes", [])
        data.setdefault("backfilled", [])
        data.setdefault("last_by_code", {})
        data.setdefault("rows", 0)
        return data

    def _write_manifest(self, data):
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_bytes(self.manifest_path, payload.encode("utf-8"))

    def save_bars(self, bars, complete_codes=None):
        """合并一批行情；已有 code+date 保持不变，仅插入缺失记录。

        complete_codes 表示这些代码已成功执行过从 2024 年开始的全量抓取，
        后续 update 可以安全地只抓增量。返回新增行数、分片数与无效行数。
        """
        grouped = {}
        invalid = 0
        for raw in bars:
            try:
                bar = _bar(raw)
                if bar["date"] < START_DATE:
                    continue
                grouped.setdefault(bar["date"], {})[bar["code"]] = bar
            except (TypeError, ValueError):
                invalid += 1

        manifest = self._manifest()
        codes = set(manifest["codes"])
        last_by_code = dict(manifest["last_by_code"])
        inserted = shards = 0
        for date in sorted(grouped):
            incoming = grouped[date]
            existing = self._read_shard(date)
            new_codes = sorted(set(incoming) - set(existing))
            if not new_codes:
                continue
            # 已存在记录不覆盖；只追加当前分片里缺失的股票。
            merged = dict(existing)
            for code in new_codes:
                merged[code] = incoming[code]
            payload = _gzip_csv([merged[c] for c in sorted(merged)])
            _atomic_bytes(self.shard_path(date), payload)
            inserted += len(new_codes)
            shards += 1

        for date, rows in grouped.items():
            for code in rows:
                codes.add(code)
                if date > last_by_code.get(code, ""):
                    last_by_code[code] = date
        complete = {c for c in (complete_codes or []) if c in codes}
        dates = self._dates()
        manifest.update({
            "codes": sorted(codes),
            "backfilled": sorted(set(manifest["backfilled"]) | complete),
            "last_by_code": last_by_code,
            "rows": int(manifest["rows"]) + inserted,
            "trading_days": len(dates),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        self._write_manifest(manifest)
        return {"inserted": inserted, "shards_written": shards, "invalid": invalid,
                "backfilled": len(complete)}

    def last_date(self, code):
        code = _code(code)
        return self._manifest()["last_by_code"].get(code)

    def is_backfilled(self, code):
        return _code(code) in set(self._manifest()["backfilled"])

    def coverage(self):
        data = self._manifest()
        return {
            "root": self.root,
            "start_date": START_DATE,
            "first_date": data.get("first_date"),
            "last_date": data.get("last_date"),
            "trading_days": data.get("trading_days", 0),
            "stocks": len(data["codes"]),
            "backfilled_stocks": len(data["backfilled"]),
            "rows": data.get("rows", 0),
            "updated_at": data.get("updated_at"),
        }

    def load_many(self, codes=None, start=None, end=None, count=None):
        """一次顺序扫描分片，返回 {code: [bars]}；全市场回测应避免逐票扫描。

        count 可让每只股票只保留最近 N 根，避免长历史全市场回测占用过多内存。
        """
        selected = None if codes is None else {_code(c) for c in codes}
        if selected is not None and not selected:
            return {}
        n = max(0, int(count)) if count is not None else None

        def container():
            return deque(maxlen=n) if n is not None else []

        out = {code: container() for code in selected} if selected is not None else {}
        for date in self._dates(start=start, end=end):
            for code, stored in self._read_shard(date).items():
                if selected is not None and code not in selected:
                    continue
                bar = {k: stored[k] for k in BAR_FIELDS}
                if code not in out:
                    out[code] = container()
                out[code].append(bar)
        if n is not None:
            out = {code: list(bars) for code, bars in out.items()}
        return out

    def load_bars(self, code, start=None, end=None, count=None, required=False):
        code = _code(code)
        bars = self.load_many([code], start=start, end=end, count=count).get(code, [])
        if required and not bars:
            raise MarketDataError(
                "本地行情缺失: code=%s range=%s~%s root=%s；"
                "请先运行 python3 common/market_store.py update（离线模式不会访问外网）"
                % (code, start or START_DATE, end or "latest", self.root))
        return bars

    def materialize(self, codes, dest_dir, start=None, end=None, count=None):
        """物化旧格式 per-code CSV；内容未变化时不改写文件。"""
        selected = [_code(c) for c in codes]
        data = self.load_many(selected, start=start, end=end, count=count)
        written = unchanged = 0
        missing = []
        os.makedirs(dest_dir, exist_ok=True)
        for code in selected:
            bars = data.get(code) or []
            if not bars:
                missing.append(code)
                continue
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, fieldnames=BAR_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(bars)
            payload = text.getvalue().encode("utf-8")
            path = os.path.join(dest_dir, code + ".csv")
            try:
                with open(path, "rb") as fh:
                    same = fh.read() == payload
            except OSError:
                same = False
            if same:
                unchanged += 1
            else:
                _atomic_bytes(path, payload)
                written += 1
        return {"written": written, "unchanged": unchanged, "missing": missing}


def _update(args):
    import common_lib as cm

    universe = cm.get_market_universe(refresh=True)
    if args.codes:
        wanted = {_code(c) for c in args.codes.split(",") if c.strip()}
        universe = [u for u in universe if u["code"] in wanted]
    if not universe:
        raise MarketDataError("全市场代码表为空，未执行行情更新")
    materialize = [u["code"] for u in universe] if args.materialize else None
    cm.fetch_klines_batch(
        universe, workers=args.workers, budget=args.budget,
        force=args.command == "backfill" or args.full,
        materialize_codes=materialize,
    )
    print(json.dumps(cm.MARKET_STORE.coverage(), ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="2024 年起全市场日 K 本地库")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("update", "backfill"):
        p = sub.add_parser(name, help="增量更新" if name == "update" else "重新检查完整历史")
        p.add_argument("--codes", help="仅更新逗号分隔的代码（调试用）")
        p.add_argument("--workers", type=int, default=4)
        p.add_argument("--budget", type=int, default=7200)
        p.add_argument("--full", action="store_true", help="对全部代码重新请求 2024 年起历史")
        p.add_argument("--materialize", action="store_true", help="同时重建 common/state/klines CSV")
    p = sub.add_parser("load", help="严格离线读取单只股票并输出 JSON")
    p.add_argument("code")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--count", type=int)
    p = sub.add_parser("coverage", help="查看本地库覆盖统计")
    args = parser.parse_args(argv)

    if args.command in ("update", "backfill"):
        return _update(args)
    store = MarketStore()
    if args.command == "load":
        bars = store.load_bars(args.code, start=args.start, end=args.end,
                               count=args.count, required=True)
        print(json.dumps(bars, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(store.coverage(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MarketDataError as exc:
        raise SystemExit("[market_store] %s" % exc)
