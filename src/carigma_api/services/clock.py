"""The reader's clock: India Standard Time.

Carigma's users are in India and the server runs in UTC. Anything said in
words about the time of day — a greeting, a receipt's "SUN 22:06" — is said
in IST, or it is wrong by five and a half hours: "Afternoon, Ravi" at 9 pm.

**Not yet the product's DAY.** Which date "today" is — the week a post falls
in, the day the strip rings — is still the UTC date everywhere, so it rolls
over at 05:30 IST rather than midnight. Moving one surface alone would put
Today and Posts on different weeks for the first hours of every Monday, so the
day boundary moves everywhere at once or not at all (PROJECT_STATUS 0.070).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

#: No daylight saving in India, so a fixed offset is exact — and `zoneinfo`
#: needs the tzdata package on Windows, where this is developed.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def local(moment: datetime) -> datetime:
    """`moment` on the reader's clock."""
    return moment.astimezone(IST)


__all__ = ["IST", "local"]
