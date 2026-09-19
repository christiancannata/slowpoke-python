from django.db import models


class Customer(models.Model):
    name = models.CharField(max_length=50)
    email = models.CharField(max_length=100)

    class Meta:
        app_label = "django_shop"


class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    total = models.IntegerField()

    class Meta:
        app_label = "django_shop"
