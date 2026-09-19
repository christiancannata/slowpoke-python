from asgiref.sync import sync_to_async
from django.http import Http404, HttpResponse, JsonResponse

from .models import Customer, Order


def order_list(request):
    orders = list(Order.objects.order_by("id"))  # origin: list
    names = [Customer.objects.get(pk=o.customer_id).name for o in orders]  # origin: n+1
    return JsonResponse({"names": names})


def order_detail(request, pk):
    order = Order.objects.filter(pk=pk).first()  # origin: detail
    if order is None:
        raise Http404("no order")
    return JsonResponse({"total": order.total})


def legacy(request, slug):
    return HttpResponse(str(Customer.objects.count()))  # origin: legacy


def lookup(request):
    customer = Customer.objects.filter(email=request.GET["email"]).first()  # origin: lookup
    return HttpResponse(customer.name if customer else "")


def boom(request):
    Order.objects.count()  # origin: boom
    raise RuntimeError("broken view")


async def async_orders(request):
    count = await sync_to_async(Order.objects.count)()  # origin: async
    return HttpResponse(str(count))
