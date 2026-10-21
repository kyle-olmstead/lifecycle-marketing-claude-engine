"""
Deterministic scenario data for engine/deliverability/monitor.py: six days of a warm-up that goes wrong in
the ways the monitor exists to catch.

Nothing here is real: recipient domains are real mailbox providers (so the provider mapping has something
to map) but there are no addresses, only domains, and the three business domains use the reserved
.example top-level domain (RFC 2606). Server texts are the documented wording from
examples/deliverability/bounces/corpus.json.

The story, 1 to 6 October 2026, volumes 1,000 to 3,500 a day:

  day 3  Gmail rejects two messages with 550 5.7.26 (sender unauthenticated): an authentication failure, STOP.
  day 4  Microsoft deferrals (451 4.7.650, rate limited on IP reputation) start to rise.
  day 5  Microsoft deferrals rise for the second day running: DROP BACK. Yahoo complaints reach 3 of about
         1,050 delivered (0.29%), over the 0.1% line but under 0.3%: DROP BACK.
  day 6  Microsoft deferrals climb again (DROP BACK); Yahoo is clean but inside its two-day hold; Gmail has
         had its two clean days. A Google Workspace recipient defers on an allowlist rule: a policy block,
         reported but not counted as reputation.

Plus a handful of hard bounces every day, an occasional full mailbox, and unsubscribes.

    python3 examples/deliverability/monitor/make_data.py     # writes message_events.csv, users.csv, track_events.csv beside it
"""
from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
SEED = 20261001
DAYS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06"]
VOLUME = [1000, 1500, 1500, 2500, 3000, 3500]
# provider group: (share of the day's volume, its recipient domains)
GROUPS: Dict[str, Tuple[float, List[str]]] = {
    "google": (0.35, ["gmail.com"]),
    "microsoft": (0.20, ["outlook.com", "hotmail.com", "live.com"]),
    "yahoo": (0.35, ["yahoo.com", "aol.com", "ymail.com"]),
    "apple": (0.05, ["icloud.com", "me.com"]),
    "business": (0.05, ["acme-corp.example", "globex.example", "initech.example"]),
}
HARD = {
    "google": ("5.1.1", "550 5.1.1 The email account that you tried to reach does not exist."),
    "microsoft": ("5.1.10", "550 5.1.10 Recipient not found"),
    "yahoo": ("", "554 delivery error: dd This user doesn't have a yahoo.com account"),
    "apple": ("5.1.1", "550 5.1.1 <recipient>: Recipient address rejected: User unknown"),
    "business": ("5.1.1", "550 5.1.1 <recipient>: Recipient address rejected: User unknown"),
}
AUTH_BLOCK = ("5.7.26", "550 5.7.26 This email has been blocked because the sender is unauthenticated.")
MS_DEFERRAL = ("4.7.650", "451 4.7.650 The mail server [192.0.2.10] has been temporarily rate limited due to IP reputation.")
GMAIL_FULL = ("4.2.2", "452 4.2.2 The recipient's inbox is out of storage space.")
ALLOWLIST = ("4.7.0", "421 4.7.0 IP not in whitelist for RCPT domain, closing connection.")
MS_DEFERRALS = [2, 3, 1, 7, 25, 70]           # rates 1.0%, 1.0%, 0.3%, 1.4%, 4.0%, 9.1% of attempts: climbing on days 4, 5 and 6
YAHOO_COMPLAINTS = [0, 0, 1, 1, 3, 1]          # day 5: 3 of about 1,047 delivered
MS_COMPLAINTS = [0, 0, 0, 0, 1, 0]
COLUMNS = ["event_id", "user_id", "channel", "event_name", "event_at", "recipient_domain", "smtp_status", "diagnostic"]


def main() -> None:
    rng = random.Random(SEED)
    rows: List[Dict[str, str]] = []
    pools: Dict[str, int] = {g: 0 for g in GROUPS}

    def add(user: str, event: str, day: str, second: int, domain: str, status: Optional[Tuple[str, str]] = None) -> None:
        rows.append({"event_id": f"d{len(rows) + 1:06d}", "user_id": user, "channel": "email", "event_name": event,
                     "event_at": f"{day} {9 + second // 3600:02d}:{(second // 60) % 60:02d}:{second % 60:02d}",
                     "recipient_domain": domain, "smtp_status": status[0] if status else "", "diagnostic": status[1] if status else ""})

    for d, (day, volume) in enumerate(zip(DAYS, VOLUME)):
        for group, (share, domains) in GROUPS.items():
            sends = round(volume * share)
            hard = round(sends * 0.003) + rng.randint(0, 1)
            delivered_users: List[Tuple[str, str]] = []
            for i in range(sends):
                pools[group] += 1
                user = f"w_{group[:2]}{pools[group]:05d}"
                domain = domains[i % len(domains)]
                second = (i * 7) % 28800
                if i < hard:
                    add(user, "bounced", day, second, domain, HARD[group])
                    continue
                if group == "google" and day == "2026-10-03" and i < hard + 2:
                    add(user, "bounced", day, second, domain, AUTH_BLOCK)
                    continue
                add(user, "delivered", day, second, domain)
                delivered_users.append((user, domain))
            # deferrals are extra delivery attempts on messages that were later delivered
            if group == "microsoft":
                for k in range(MS_DEFERRALS[d]):
                    user, domain = delivered_users[k]
                    add(user, "deferred", day, 30 + k, domain, MS_DEFERRAL)
            if group == "google":
                user, domain = delivered_users[-1]
                add(user, "deferred", day, 60, domain, GMAIL_FULL)
            if group == "business" and day == "2026-10-06":
                add(delivered_users[0][0], "deferred", day, 90, "acme-corp.example", ALLOWLIST)
            complaints = YAHOO_COMPLAINTS[d] if group == "yahoo" else MS_COMPLAINTS[d] if group == "microsoft" else 0
            for k in range(complaints):
                user, domain = delivered_users[-(k + 2)]
                add(user, "complained", day, 28000 + k, domain)
            for k in range(round(len(delivered_users) * 0.002)):
                user, domain = delivered_users[k * 37 % len(delivered_users)]
                add(user, "unsubscribed", day, 27000 + k, domain)

    with (HERE / "message_events.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    # the loader requires users and track_events; the monitor needs neither, so they stay minimal
    with (HERE / "users.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["user_id", "email", "created_at", "email_consent_at"])
        for i in range(1, 4):
            w.writerow([f"w_go{i:05d}", f"w_go{i:05d}@example.test", "2026-09-01 00:00:00", "2026-09-01 00:00:00"])
    with (HERE / "track_events.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["event_id", "user_id", "anonymous_id", "event_name", "event_at", "properties"])
        w.writerow(["t000001", "w_go00001", "", "signed_up", "2026-09-01 00:00:00", "{}"])


if __name__ == "__main__":
    main()
