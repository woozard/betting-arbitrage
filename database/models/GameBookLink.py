from sqlalchemy import (
    BigInteger,
    Column,
    ForeignKey,
    String,
    TIMESTAMP,
    UniqueConstraint,
    func,
)

from database.config import __get_base__

Base = __get_base__()


class GameBookLink(Base):
    """Maps a book-specific game id to a canonical game row."""

    __tablename__ = "game_book_links"
    __table_args__ = (
        UniqueConstraint("bookmaker", "book_game_id", name="uq_game_book_links_book_game"),
        UniqueConstraint("canonical_game_id", "bookmaker", name="uq_game_book_links_game_book"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    created_at = Column(TIMESTAMP, nullable=False, server_default=func.now())
    updated_at = Column(
        TIMESTAMP,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    canonical_game_id = Column(
        BigInteger,
        ForeignKey("canonical_games.id", ondelete="CASCADE"),
        nullable=False,
    )
    bookmaker = Column(String(100), nullable=False)
    book_game_id = Column(String(50), nullable=False)
    team_1 = Column(String(100), nullable=False)
    team_2 = Column(String(100), nullable=False)

    def __init__(
        self,
        canonical_game_id,
        bookmaker,
        book_game_id,
        team_1,
        team_2,
    ):
        self.canonical_game_id = canonical_game_id
        self.bookmaker = bookmaker
        self.book_game_id = book_game_id
        self.team_1 = team_1
        self.team_2 = team_2


def __get_instance__():
    return Base
