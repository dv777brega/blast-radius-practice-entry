"""Deterministic worker assignment. All arithmetic is in scorer milliseconds."""
from functools import lru_cache


class Scheduler:
    def __init__(self, tests):
        self.weights = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
        self.plan = lru_cache(maxsize=128)(self._plan)

    def _plan(self, chosen):
        if not chosen:
            return 0, []
        weights = self.weights
        order = sorted(chosen, key=lambda number: (-weights[number], number))
        total = sum(weights[n] for n in order)
        best = None
        for k in range(1, min(8, len(order)) + 1):
            floor = max(weights[order[0]], (total + k - 1) // k)
            if best is not None and floor + 2000 * k >= best[0]:
                continue
            bins = [[n] for n in order[:k]]
            loads = [weights[n] for n in order[:k]]
            for n in order[k:]:
                j = loads.index(min(loads))
                bins[j].append(n)
                loads[j] += weights[n]
            for _ in range(8):
                peak = max(loads)
                if peak == floor:
                    break
                heavy = loads.index(peak)
                light = loads.index(min(loads))
                gap = loads[heavy] - loads[light]
                if gap <= 1 or len(bins[heavy]) < 2:
                    break
                movable = [n for n in bins[heavy] if 0 < weights[n] < gap]
                if movable:
                    n = min(movable, key=lambda n: abs(gap - 2 * weights[n]))
                    bins[heavy].remove(n)
                    bins[light].append(n)
                    loads[heavy] -= weights[n]
                    loads[light] += weights[n]
                    continue
                # A swap can improve a partition even when no single test can move.
                swap = next(((a, b) for a in bins[heavy][:32] for b in bins[light][:32]
                             if 0 < weights[a] - weights[b] < gap), None)
                if swap is None:
                    break
                a, b = swap
                bins[heavy].remove(a)
                bins[light].remove(b)
                bins[heavy].append(b)
                bins[light].append(a)
                loads[heavy] += weights[b] - weights[a]
                loads[light] += weights[a] - weights[b]
            cost = max(loads) + 2000 * k
            if best is None or cost < best[0]:
                best = cost, bins
        return best
