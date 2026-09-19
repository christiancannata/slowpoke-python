from django.urls import include, path, re_path

from . import views

api = [path("orders/<int:pk>/", views.order_detail)]

urlpatterns = [
    path("orders/", views.order_list),
    path("api/", include(api)),
    re_path(r"^legacy/(?P<slug>[-\w]+)/$", views.legacy),
    path("customers/lookup", views.lookup),
    path("boom/", views.boom),
    path("async/orders/", views.async_orders),
]
