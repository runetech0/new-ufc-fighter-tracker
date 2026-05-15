from dataclasses import dataclass, field


@dataclass
class Athlete:
    name: str
    nickname: str | None
    weight_class: str | None
    record: str | None
    profile_url: str | None
    image_url: str | None = field(default=None)

    def __str__(self) -> str:
        nickname = f' "{self.nickname}"' if self.nickname else ""
        return (
            f"{self.name}{nickname} | {self.weight_class} | "
            f"{self.record} | {self.profile_url}"
        )
