from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_session_from_resources_reaches_the_database(session: AsyncSession) -> None:
    result = await session.execute(text("SELECT 1"))

    assert result.scalar_one() == 1
