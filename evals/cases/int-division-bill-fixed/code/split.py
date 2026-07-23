def split_bill(total_pence, people):
    """Divide a bill evenly between people, in whole pence."""
    base, extra = divmod(total_pence, people)
    return [base + 1] * extra + [base] * (people - extra)
