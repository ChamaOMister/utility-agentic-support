import pytest
import sqlalchemy.exc
from sqlalchemy import text


async def test_support_app_denied_on_billing_schema(support_test_engine):
    async with support_test_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            await conn.execute(text("CREATE TABLE billing.probe (id int)"))
    async with support_test_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.ProgrammingError, match="permission denied"):
            await conn.execute(text("SELECT 1 FROM billing.customers LIMIT 1"))
