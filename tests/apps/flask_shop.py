from flask import Blueprint, Flask, abort, jsonify, request

import slowpoke.flask
import slowpoke.sqlalchemy

from . import sa_shop


def create_app(engine):
    app = Flask(__name__)

    @app.before_request
    def early_hook():
        if request.args.get("blocked"):
            return "blocked", 403  # answers before the view: the request is traced all the same

    slowpoke.flask.init_app(app)
    slowpoke.sqlalchemy.instrument(engine)

    @app.route("/orders")
    def orders():
        return jsonify(sa_shop.customer_names(engine))

    @app.route("/api/orders/<int:order_id>", methods=["GET", "PUT"])
    def order(order_id):
        total = sa_shop.order_total(engine, order_id)
        if total is None:
            abort(404)
        return jsonify(total=total)

    @app.route("/customers/lookup")
    def lookup():
        return sa_shop.find_by_email(engine, request.args["email"]) or ""

    @app.route("/boom")
    def boom():
        sa_shop.order_total(engine, 1)
        raise RuntimeError("broken view")

    admin = Blueprint("admin", __name__, url_prefix="/admin")

    @admin.route("/stats/<name>")
    def stats(name):
        return str(sa_shop.order_total(engine, 2))

    app.register_blueprint(admin)
    return app
