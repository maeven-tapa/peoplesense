from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.forms import SetPasswordForm, PasswordChangeForm
from django.contrib.auth.views import LoginView
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST
from . import appliance
from .forms import WifiSetupForm, ApplianceCameraForm, TripwireForm, CameraModeForm
from .models import PortalSettings, Device
from .views import portal_access

STEPS=['security','network','camera','line']
LABELS=['Administrator password','Wi-Fi AP name','USB camera','Camera mode & line']

class ApplianceLoginView(LoginView):
    template_name='portal/login.html'
    redirect_authenticated_user=True
    def get_context_data(self,**kwargs):
        context=super().get_context_data(**kwargs)
        context['first_use']=PortalSettings.load().setup_stage==0
        return context
    def get_success_url(self):
        return '/setup/' if PortalSettings.load().setup_stage<4 and not self.request.session.get('setup_deferred') else '/dashboard/'
    def form_valid(self,form):
        if not form.get_user().is_staff:
            form.add_error(None,'Use the device administrator account.')
            return self.form_invalid(form)
        return super().form_valid(form)

@portal_access
@require_POST
def skip_setup(request):
    request.session['setup_deferred']=True
    messages.success(request,'Setup skipped for now. You can continue from Quick setup whenever you are ready.')
    return redirect('dashboard')

@portal_access
def setup(request,step=None):
    profile=PortalSettings.load()
    if step not in STEPS:
        return redirect('setup-step',step=STEPS[min(profile.setup_stage,3)])
    index=STEPS.index(step)
    if index>profile.setup_stage:
        return redirect('setup')
    device=appliance.camera()
    data=request.POST if request.method=='POST' else None
    form=None
    if step=='security':
        form=(PasswordChangeForm if profile.setup_stage>=1 else SetPasswordForm)(request.user,data)
    elif step=='network':
        form=WifiSetupForm(data,instance=profile)
    elif step=='camera':
        form=ApplianceCameraForm(data,instance=device)
    if request.method=='POST':
        if step=='line':
            if not device or (device.camera_mode==Device.Mode.TRIPWIRE and not profile.line_configured) or not request.POST.get('line_confirmed'):
                messages.error(request,'Save your camera mode and, for Tripwire, position and save the line. Then confirm that it is ready.')
            else:
                PortalSettings.objects.filter(pk=1).update(setup_stage=4)
                messages.success(request,'Setup complete. Your camera dashboard is ready.')
                return redirect('dashboard')
        elif form.is_valid():
            with transaction.atomic():
                if step=='security':
                    user=form.save()
                    update_session_auth_hash(request,user)
                elif step=='network':
                    form.save()
                elif step=='camera':
                    from vision.service import stop_camera
                    stop_camera(device.pk)
                    camera=form.save(commit=False)
                    camera.config_version=F('config_version')+1
                    camera.save(update_fields=[*form.Meta.fields,'source_type','temperature_enabled','config_version'])
                    PortalSettings.objects.filter(pk=1).update(line_configured=False,setup_stage=3)
                PortalSettings.objects.filter(pk=1,setup_stage__lt=index+1).update(setup_stage=index+1)
            messages.success(request,'Settings saved.')
            return redirect('setup-step',step=STEPS[index+1])
    context={'page':'setup','title':'Quick setup','step':step,'step_index':index,'form':form,'device':device,
             'steps':list(zip(STEPS,LABELS)), 'setup_complete':profile.setup_stage>=4}
    if step=='line' and device:
        from vision.service import camera_state
        context.update(tripwire_form=TripwireForm(instance=device),mode_form=CameraModeForm(instance=device),camera_state=camera_state(device),entries=0,
            tripwire_data={'ax':device.line_ax,'ay':device.line_ay,'bx':device.line_bx,'by':device.line_by,'entry_side':device.entry_side,'tolerance':device.line_tolerance})
    return render(request,'portal/setup.html',context)
