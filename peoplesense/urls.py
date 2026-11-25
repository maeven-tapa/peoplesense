from django.contrib.auth import views as auth_views
from django.urls import include, path
from portal.setup import ApplianceLoginView

urlpatterns = [
    path('', ApplianceLoginView.as_view(), name='login'),
    path('login/', ApplianceLoginView.as_view(), name='login-alias'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('', include('portal.urls')),
]
