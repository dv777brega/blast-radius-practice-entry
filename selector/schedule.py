"""Deterministic worker assignment. All arithmetic is in scorer milliseconds."""
from functools import lru_cache


class Scheduler:
    def __init__(self, tests):
        self.weights = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
        self.plan = lru_cache(maxsize=256)(self._plan)

    def _pack_lpt(self, order, k):
        weights = self.weights
        bins = [[n] for n in order[:k]]
        loads = [weights[n] for n in order[:k]]
        for n in order[k:]:
            j = loads.index(min(loads))
            bins[j].append(n)
            loads[j] += weights[n]
        return bins, loads

    def _pack_bfd(self, order, k, capacity):
        weights = self.weights
        bins = [[] for _ in range(k)]
        loads = [0] * k
        for n in order:
            w = weights[n]
            best_j = -1
            best_rem = capacity + 1
            for j in range(k):
                rem = capacity - (loads[j] + w)
                if 0 <= rem < best_rem:
                    best_rem = rem
                    best_j = j
            if best_j < 0:
                best_j = loads.index(min(loads))
            bins[best_j].append(n)
            loads[best_j] += w
        return bins, loads

    def _refine(self, bins, loads, k, floor):
        weights = self.weights
        if k <= 1:
            return
        for _ in range(32):
            peak = max(loads)
            if peak == floor:
                break
            heavy = loads.index(peak)
            light = loads.index(min(loads))
            gap = loads[heavy] - loads[light]
            if gap <= 1:
                break

            best_move = None
            best_gain = 0
            for target_idx in range(k):
                if target_idx == heavy:
                    continue
                bin_gap = loads[heavy] - loads[target_idx]
                if bin_gap <= 1:
                    continue
                for n in bins[heavy]:
                    w = weights[n]
                    if 0 < w < bin_gap:
                        new_pair_max = max(loads[heavy] - w, loads[target_idx] + w)
                        gain = loads[heavy] - new_pair_max
                        if gain > best_gain:
                            best_gain = gain
                            best_move = (target_idx, n)
            if best_move is not None:
                target_idx, n = best_move
                bins[heavy].remove(n)
                bins[target_idx].append(n)
                loads[heavy] -= weights[n]
                loads[target_idx] += weights[n]
                continue

            best_swap = None
            for target_idx in range(k):
                if target_idx == heavy:
                    continue
                bin_gap = loads[heavy] - loads[target_idx]
                if bin_gap <= 1:
                    continue
                for a in bins[heavy]:
                    wa = weights[a]
                    for b in bins[target_idx]:
                        diff = wa - weights[b]
                        if 0 < diff < bin_gap:
                            new_pair_max = max(loads[heavy] - diff, loads[target_idx] + diff)
                            gain = loads[heavy] - new_pair_max
                            if gain > best_gain:
                                best_gain = gain
                                best_swap = (target_idx, a, b)
            if best_swap is None:
                break
            target_idx, a, b = best_swap
            bins[heavy].remove(a)
            bins[target_idx].remove(b)
            bins[heavy].append(b)
            bins[target_idx].append(a)
            loads[heavy] += weights[b] - weights[a]
            loads[target_idx] += weights[a] - weights[b]

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
            bins, loads = self._pack_lpt(order, k)
            self._refine(bins, loads, k, floor)
            if max(loads) > floor and k > 1:
                bins2, loads2 = self._pack_bfd(order, k, floor)
                self._refine(bins2, loads2, k, floor)
                if max(loads2) < max(loads):
                    bins, loads = bins2, loads2
            cost = max(loads) + 2000 * k
            if best is None or cost < best[0]:
                best = cost, bins
        return best
