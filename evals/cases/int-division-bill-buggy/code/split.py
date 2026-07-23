def split_bill(total_pence, people):
    """Divide a bill evenly between people, in whole pence."""
    return [total_pence // people] * people
