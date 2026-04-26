from starlette.requests import Request

from app.main import login_form


async def test_login_page_renders_with_current_starlette_template_api():
    request = Request({"type": "http", "method": "GET", "path": "/login", "headers": []})

    response = await login_form(request)

    assert response.status_code == 200
    assert response.template.name == "login.html"
