//! Times shown in the viewer's LOCAL time zone (the gateway writes UTC ISO
//! 8601). No date crate: the offset comes from the C library's
//! `localtime_r` (`tm_gmtoff`) on Unix; elsewhere times stay in UTC and
//! say so.

/// Seconds since the Unix epoch for an ISO 8601 timestamp
/// (`2026-09-30T14:05:12+00:00`, `…Z`, fractional seconds allowed; no
/// offset = UTC). None when it does not parse.
pub fn parse_iso_epoch(ts: &str) -> Option<i64> {
    let (date, rest) = ts.trim().split_once('T')?;
    let mut d = date.split('-');
    let y: i64 = d.next()?.parse().ok()?;
    let m: i64 = d.next()?.parse().ok()?;
    let day: i64 = d.next()?.parse().ok()?;
    let (clock, offset) = match rest.find(['+', '-', 'Z']) {
        Some(i) => (&rest[..i], &rest[i..]),
        None => (rest, ""),
    };
    let mut c = clock.split(':');
    let h: i64 = c.next()?.parse().ok()?;
    let mi: i64 = c.next()?.parse().ok()?;
    let s: i64 = c
        .next()
        .map(|v| v.split('.').next().unwrap_or("0"))
        .unwrap_or("0")
        .parse()
        .ok()?;
    let off = match offset {
        "" | "Z" => 0,
        o => {
            let sign = if o.starts_with('-') { -1 } else { 1 };
            let body = &o[1..];
            let (oh, om) = body
                .split_once(':')
                .unwrap_or((body.get(..2)?, body.get(2..).unwrap_or("0")));
            sign * (oh.parse::<i64>().ok()? * 3600 + om.parse::<i64>().unwrap_or(0) * 60)
        }
    };
    if !(1..=12).contains(&m) || !(1..=31).contains(&day) {
        return None;
    }
    Some(days_from_civil(y, m, day) * 86_400 + h * 3600 + mi * 60 + s - off)
}

/// Howard Hinnant's days_from_civil.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let mp = (m + 9) % 12;
    let doy = (153 * mp + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(m <= 2), m, d)
}

/// The viewer's UTC offset (seconds) at `epoch`.
#[cfg(unix)]
pub fn local_offset(epoch: i64) -> i64 {
    let t = epoch as libc::time_t;
    let mut tm: libc::tm = unsafe { std::mem::zeroed() };
    // SAFETY: localtime_r writes only into `tm`, which we own.
    let ok = unsafe { !libc::localtime_r(&t, &mut tm).is_null() };
    if ok {
        tm.tm_gmtoff as i64
    } else {
        0
    }
}

#[cfg(not(unix))]
pub fn local_offset(_epoch: i64) -> i64 {
    0
}

/// (`YYYY-MM-DD`, `HH:MM`) of `epoch` shifted by `offset` seconds.
pub fn parts_at(epoch: i64, offset: i64) -> (String, String) {
    let t = epoch + offset;
    let (y, m, d) = civil_from_days(t.div_euclid(86_400));
    let secs = t.rem_euclid(86_400);
    (
        format!("{y:04}-{m:02}-{d:02}"),
        format!("{:02}:{:02}", secs / 3600, (secs % 3600) / 60),
    )
}

/// (`YYYY-MM-DD`, `HH:MM`) of an ISO timestamp in the viewer's local time.
pub fn local_parts(ts: &str) -> Option<(String, String)> {
    let e = parse_iso_epoch(ts)?;
    Some(parts_at(e, local_offset(e)))
}

/// Today's date in the viewer's local time.
pub fn local_today() -> String {
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    parts_at(now, local_offset(now)).0
}

/// `HH:MM` of an ISO timestamp in local time (the input unchanged when it
/// does not parse — never a made-up time).
pub fn local_hm(ts: &str) -> String {
    local_parts(ts)
        .map(|(_, hm)| hm)
        .unwrap_or_else(|| ts.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_utc_and_offsets_to_the_same_instant() {
        let a = parse_iso_epoch("2026-10-01T12:05:00Z").unwrap();
        assert_eq!(parse_iso_epoch("2026-10-01T14:05:00+02:00"), Some(a));
        assert_eq!(parse_iso_epoch("2026-10-01T12:05:00.123456+00:00"), Some(a));
        assert_eq!(parse_iso_epoch("2026-10-01T07:05:00-05:00"), Some(a));
        assert_eq!(a, 1_790_856_300);
        assert_eq!(parse_iso_epoch("yesterday"), None);
    }

    #[test]
    fn parts_shift_by_the_offset_across_midnight() {
        let e = parse_iso_epoch("2026-09-30T23:30:00Z").unwrap();
        assert_eq!(parts_at(e, 0), ("2026-09-30".into(), "23:30".into()));
        assert_eq!(parts_at(e, 2 * 3600), ("2026-10-01".into(), "01:30".into()));
        assert_eq!(
            parts_at(e, -10 * 3600),
            ("2026-09-30".into(), "13:30".into())
        );
    }
}
