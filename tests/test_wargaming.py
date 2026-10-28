from unittest.mock import AsyncMock

import pytest

from wot_verify.wargaming import Wargaming


@pytest.mark.parametrize("account", [None, {}, {"nickname": ""}, {"nickname": "x" * 33}])
async def test_missing_account_or_invalid_nickname(account):
    wg = Wargaming(None, "app", "eu")
    wg.request = AsyncMock(return_value={"10": account})
    with pytest.raises(ValueError, match="wg_error"):
        await wg.nickname(10)
