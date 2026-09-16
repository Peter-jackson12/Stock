"""Small causal replay driver. Strategy receives current view and simulator.

No live broker, file mutation, price coercion, raw-v1 sequence fabrication, or
automatic closing between chunks. Caller closes once at the declared boundary.
"""


def replay_chunk(simulator, events, strategy):
    for event in events:
        view = simulator.on_event(event)
        strategy(view, simulator)
