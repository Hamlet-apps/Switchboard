from django.urls import path

from board import views

urlpatterns = [
    path('', views.workflows_page, name='workflows'),
    path('setup/', views.setup, name='setup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    path('workflows/new/', views.workflow_new, name='workflow_new'),
    path('workflows/<int:pk>/', views.builder, name='builder'),
    path('workflows/<int:pk>/save/', views.workflow_save, name='workflow_save'),
    path('workflows/<int:pk>/toggle/', views.workflow_toggle, name='workflow_toggle'),
    path('workflows/<int:pk>/test/', views.workflow_test, name='workflow_test'),
    path('workflows/<int:pk>/test-step/', views.workflow_test_step, name='workflow_test_step'),
    path('workflows/<int:pk>/run/', views.workflow_run, name='workflow_run'),
    path('workflows/<int:pk>/replay/', views.workflow_replay, name='workflow_replay'),
    path('workflows/<int:pk>/delete/', views.workflow_delete, name='workflow_delete'),
    path('workflows/<int:pk>/duplicate/', views.workflow_duplicate, name='workflow_duplicate'),
    path('workflows/<int:pk>/move/', views.workflow_move, name='workflow_move'),
    path('folders/new/', views.folder_new, name='folder_new'),
    path('folders/<int:pk>/rename/', views.folder_rename, name='folder_rename'),
    path('folders/<int:pk>/delete/', views.folder_delete, name='folder_delete'),

    path('runs/', views.runs_page, name='runs'),
    path('runs/<int:pk>/retry/', views.run_retry, name='run_retry'),

    path('buildings/', views.buildings_page, name='buildings'),
    path('buildings/connect/', views.building_connect, name='building_connect'),
    path('buildings/callback/', views.building_callback, name='building_callback'),
    path('buildings/<int:pk>/refresh/', views.building_refresh, name='building_refresh'),
    path('buildings/<int:pk>/disconnect/', views.building_disconnect, name='building_disconnect'),

    path('settings/', views.settings_page, name='settings'),

    # Called by other servers: fixed paths, no sign-in (signature or secret token instead).
    path('hooks/building/<str:token>', views.building_hook, name='building_hook'),
    path('hooks/w/<str:token>', views.workflow_hook, name='workflow_hook'),
    path('healthz', views.healthz, name='healthz'),
]
