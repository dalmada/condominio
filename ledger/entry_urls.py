from django.urls import path

from . import entry_views as views

urlpatterns = [
    path("unidades/", views.unit_list, name="unit_list"),
    path("unidades/<int:pk>/", views.unit_edit, name="unit_edit"),
    path("categorias/", views.category_list, name="category_list"),
    path("categorias/nova/", views.category_edit, name="category_create"),
    path("categorias/<int:pk>/", views.category_edit, name="category_edit"),
    path("competencias/nova/", views.period_create, name="period_create"),
    path("competencias/<int:period_id>/editar/", views.period_edit, name="period_edit"),
    path("competencias/<int:period_id>/despesas/nova/", views.expense_create, name="expense_create"),
    path("despesas/<int:pk>/editar/", views.expense_edit, name="expense_edit"),
    path("despesas/<int:pk>/remover/", views.expense_delete, name="expense_delete"),
    path("despesas/<int:pk>/anexo/", views.expense_attachment, name="expense_attachment"),
]
