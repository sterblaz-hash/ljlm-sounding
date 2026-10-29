"""Select an explicit C-LAEF 00/12 UTC sounding run without availability fallback."""
import argparse
from datetime import date, datetime, timedelta, timezone


SCHEDULES = {'5 7 * * *': ('00', 7), '5 10 * * *': ('00', 10),
             '5 19 * * *': ('12', 19), '5 22 * * *': ('12', 22)}


def choose_run(cycle='', run_date='', *, schedule='', now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if schedule:
        if schedule not in SCHEDULES:
            raise ValueError('Unexpected C-LAEF schedule')
        cycle, due_hour = SCHEDULES[schedule]
        if run_date:
            raise ValueError('Scheduled runs use the current UTC date')
    if cycle not in ('00', '12'):
        raise ValueError('Only cycles 00 and 12 are supported')
    day = date.fromisoformat(run_date) if run_date else now.date()
    if run_date and day.isoformat() != run_date:
        raise ValueError('Run date must be YYYY-MM-DD')
    run = datetime(day.year, day.month, day.day, int(cycle), tzinfo=timezone.utc)
    if schedule:
        # Scheduled events can execute after midnight; use the most recent
        # occurrence of this EXACT schedule, not the next calendar day's run.
        due = run.replace(hour=due_hour, minute=5)
        if due > now:
            run -= timedelta(days=1)
    elif not run_date and run > now:
        run -= timedelta(days=1)
    if run > now:
        raise ValueError('Run date/cycle is in the future')
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cycle', default='')
    parser.add_argument('--run-date', default='')
    parser.add_argument('--schedule', default='')
    args = parser.parse_args()
    try:
        run = choose_run(args.cycle, args.run_date, schedule=args.schedule)
    except ValueError as error:
        parser.error(str(error))
    print(run.strftime('%Y-%m-%dT%H:%M:%SZ'))


if __name__ == '__main__':
    main()
