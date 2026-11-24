from django.urls import include, path
from ledger import access, views

urlpatterns = [
    path("entrar/", access.AdminLogin.as_view(), name="login"),
    path("sair/", access.logout_view, name="logout"),
    path("primeiro-acesso/", access.first_access, name="first_access"),
    path("", views.dashboard, name="dashboard"),
    path("competencias/<int:period_id>/unidades/<int:unit_id>/", views.unit_breakdown, name="unit_breakdown"),
    path("competencias/<int:pk>/", views.period_detail, name="period_detail"),
    path("competencias/<int:pk>/fechar/", views.close, name="period_close"),
    path("competencias/<int:pk>/retificar/", views.revise, name="period_revise"),
    path("historico/", views.history, name="history"),
    path("versoes/<int:pk>/despesas/<int:expense_id>/anexo/", views.archived_attachment, name="archived_attachment"),
    path("demonstrativos/<int:pk>/", views.statement, name="statement"),
    path("cobrancas/<int:period_id>/<int:unit_id>/", views.charge, name="charge"),
    path("pagamentos/<int:pk>/estornar/", views.reverse, name="payment_reverse"),
    path("", include("ledger.entry_urls")),
]
