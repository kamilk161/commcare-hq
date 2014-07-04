


class Getter(object):

    def get_value(self, item):
        raise NotImplementedError()


class SimpleGetter(object):

    def __init__(self, property_name):
        self.property_name = property_name

    def __call__(self, item):
        try:
            return getattr(item, self.property_name)
        except AttributeError:
            return None