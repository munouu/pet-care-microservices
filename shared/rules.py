"""Pure domain rules used by schedule, payment and health."""
from datetime import timedelta

def overlaps(start_a,end_a,start_b,end_b):
    """Half-open intervals [start,end); adjacent slots are allowed."""
    return start_a<end_b and start_b<end_a

def reservation_deadline(current,start):
    return min(current+timedelta(minutes=15),start)

def dose_times(first,interval_hours,count):
    return [first+timedelta(hours=interval_hours*i) for i in range(count)]
