# Poe Trade
> Торговый бот купли/продажи для игры Path of Exile.

## Стэк

    numpy
    openCV (cv2)
    pytest
    pyQt5
    win32api

## Жизненный цикл

TODO


## Tests
```sh
pytest (run all tests)
pytest -s (with i/o logging)
pytest modules/tests/test_db.py (run separate testcase)
pytest -v -m slow (run only decorated tag-mark: @pytest.mark.slow)
pytest -s -v -m "not slow" (inverse - exclude tests decorated with 'slow')
```
