from pathlib import Path

from flask import Flask

from .config import Config
from .extensions import db


def create_app(config_object=Config):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)
    if config_object is not Config:
        app.config.from_object(config_object)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    from .cli import register_commands
    from .routes import main_bp

    app.register_blueprint(main_bp)
    register_commands(app)

    return app
