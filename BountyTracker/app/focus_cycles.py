"""A saved focus cycle and the immutable phase plan for one run."""

from dataclasses import asdict, dataclass
import json


@dataclass(frozen=True)
class Phase:
    mode: str
    title: str
    seconds: int
    round_number: int


@dataclass(frozen=True)
class Cycle:
    name: str
    focus: int
    short: int
    long: int
    rounds: int
    auto: int

    @classmethod
    def from_row(cls, row):
        return cls(row['name'], row['focus_minutes'], row['short_minutes'],
                   row['long_minutes'], row['cycle_size'], row['auto_advance'])

    def phases(self):
        phases = []
        for number in range(1, self.rounds + 1):
            phases.append(Phase('pomodoro', 'Focus', self.focus * 60, number))
            last = number == self.rounds
            minutes = self.long if last else self.short
            if minutes:
                phases.append(Phase('break', 'Long break' if last else 'Short break', minutes * 60, number))
        return tuple(phases)

    def serialize(self):
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def restore(cls, text):
        return cls(**json.loads(text)) if text else None
