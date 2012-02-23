from django.shortcuts import redirect

import jingo
import waffle

import amo
from amo.decorators import login_required
from addons.forms import CategoryFormSet, DeviceTypeForm
from addons.models import Addon, AddonUser
from market.models import AddonPaymentData
from mkt.developers import tasks
from mkt.developers.decorators import dev_required
from mkt.developers.forms import PaypalSetupForm, PaypalPaymentData
from mkt.submit.forms import AppDetailsBasicForm, PreviewFormSet
from mkt.submit.models import AppSubmissionChecklist
import paypal
from files.models import Platform
from users.models import UserProfile
from . import forms
from .decorators import submit_step


@login_required
def submit(request):
    """Determine which step to redirect user to."""
    # If dev has already agreed, continue to next step.
    user = UserProfile.objects.get(pk=request.user.id)
    if user.read_dev_agreement:
        # TODO: Have decorator redirect to next step.
        return redirect('submit.app.manifest')
    else:
        return redirect('submit.app.terms')


@login_required
@submit_step('terms')
def terms(request):
    # If dev has already agreed, continue to next step.
    # TODO: When this code is finalized, use request.amo_user instead.
    user = UserProfile.objects.get(pk=request.user.id)
    if user.read_dev_agreement:
        # TODO: Have decorator redirect to next step.
        return redirect('submit.app.manifest')

    agreement_form = forms.DevAgreementForm({'read_dev_agreement': True},
                                            instance=user)
    if request.POST and agreement_form.is_valid():
        agreement_form.save()
        return redirect('submit.app.manifest')
    return jingo.render(request, 'submit/terms.html', {
        'step': 'terms',
        'agreement_form': agreement_form,
    })


@login_required
@submit_step('manifest')
def manifest(request):
    # TODO: Have decorator handle the redirection.
    user = UserProfile.objects.get(pk=request.user.id)
    if not user.read_dev_agreement:
        # And we start back at one...
        return redirect('submit.app')

    form = forms.NewWebappForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        data = form.cleaned_data

        plats = [Platform.objects.get(id=amo.PLATFORM_ALL.id)]
        addon = Addon.from_upload(data['upload'], plats)
        tasks.fetch_icon.delay(addon)
        AddonUser(addon=addon, user=request.amo_user).save()

        # Checking it once. Checking it twice.
        AppSubmissionChecklist.objects.create(addon=addon, terms=True,
                                              manifest=True)

        return redirect('submit.app.details', addon.app_slug)

    return jingo.render(request, 'submit/manifest.html', {
        'step': 'manifest',
        'form': form,
    })


@dev_required
@submit_step('details')
def details(request, addon_id, addon):
    # Name, Slug, Summary, Description, Privacy Policy,
    # Homepage URL, Support URL, Support Email.
    form_basic = AppDetailsBasicForm(request.POST or None, instance=addon,
                                     request=request)
    form_cats = CategoryFormSet(request.POST or None, addon=addon,
                                request=request)
    form_devices = DeviceTypeForm(request.POST or None, addon=addon)
    form_previews = PreviewFormSet(request.POST or None,
                                   prefix='files',
                                   queryset=addon.previews.all())

    forms = {
        'form_basic': form_basic,
        'form_devices': form_devices,
        'form_cats': form_cats,
        'form_previews': form_previews,
    }

    if request.POST and all(f.is_valid() for f in forms.itervalues()):
        addon = form_basic.save(addon)
        form_devices.save(addon)
        form_cats.save()
        for preview in form_previews.forms:
            preview.save(addon)
        AppSubmissionChecklist.objects.get(addon=addon).update(details=True)
        return redirect('submit.app.payments', addon.app_slug)

    ctx = {
        'step': 'details',
        'addon': addon,
    }
    ctx.update(forms)
    return jingo.render(request, 'submit/details.html', ctx)


@dev_required
@submit_step('payments')
def payments(request, addon_id, addon):
    form = forms.PremiumTypeForm(request.POST or None)
    if request.POST and form.is_valid():
        addon.update(premium_type=form.cleaned_data['premium_type'])

        if waffle.flag_is_active(request, 'advanced-payments-submission'):
            if addon.premium_type in amo.ADDON_PREMIUMS:
                return redirect('submit.app.payments.upsell', addon.app_slug)
            if addon.premium_type == amo.ADDON_FREE_INAPP:
                return redirect('submit.app.payments.paypal', addon.app_slug)

        AppSubmissionChecklist.objects.get(addon=addon).update(payments=True)
        return redirect('submit.app.done', addon.app_slug)
    return jingo.render(request, 'submit/payments.html', {
                        'step': 'payments',
                        'addon': addon,
                        'form': form
                        })


@dev_required
@submit_step('payments')
def payments_upsell(request, addon_id, addon):
    form = forms.UpsellForm(request.POST or None, request=request,
                            extra={'addon': addon,
                                   'amo_user': request.amo_user})
    if request.POST and form.is_valid():
        form.save()
        return redirect('submit.app.payments.paypal', addon.app_slug)
    return jingo.render(request, 'submit/payments-upsell.html', {
                        'step': 'payments',
                        'addon': addon,
                        'form': form
                        })


@dev_required
@submit_step('payments')
def payments_paypal(request, addon_id, addon):
    form = PaypalSetupForm(request.POST or None)
    if request.POST and form.is_valid():
        addon.update(paypal_id=form.cleaned_data['email'])
        return redirect('submit.app.payments.bounce', addon.app_slug)
    return jingo.render(request, 'submit/payments-paypal.html', {
                        'step': 'payments',
                        'addon': addon,
                        'form': form
                        })


@dev_required
@submit_step('payments')
def payments_bounce(request, addon_id, addon):
    paypal_url = paypal.get_permission_url(addon, 'submission',
                                           ['REFUND',
                                            'ACCESS_BASIC_PERSONAL_DATA',
                                            'ACCESS_ADVANCED_PERSONAL_DATA'])
    return jingo.render(request, 'submit/payments-bounce.html', {
                        'step': 'payments',
                        'paypal_url': paypal_url,
                        'addon': addon
                        })


@dev_required
@submit_step('payments')
def payments_confirm(request, addon_id, addon):
    adp, created = AddonPaymentData.objects.safer_get_or_create(addon=addon)
    form = PaypalPaymentData(request.POST or None, instance=adp)
    if request.method == 'POST' and form.is_valid():
        adp.update(**form.cleaned_data)
        AppSubmissionChecklist.objects.get(addon=addon).update(payments=True)
        return redirect('submit.app.done', addon.app_slug)

    return jingo.render(request, 'submit/payments-confirm.html', {
                        'step': 'payments',
                        'addon': addon,
                        'form': form
                        })


@dev_required
def done(request, addon_id, addon):
    # No submit step forced on this page, we don't really care.
    return jingo.render(request, 'submit/done.html', {
                        'step': 'done', 'addon': addon
                        })
