from decimal import Decimal
from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder


class ExactJSONEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return format(obj, "f")
        return super().default(obj)


class ExactJSONRenderer(JSONRenderer):
    encoder_class = ExactJSONEncoder
