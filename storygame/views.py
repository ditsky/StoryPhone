from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse
from .models import Title, StoryPage


def create_title(request):
    """View to display a form for creating a new title and save it to the database."""
    if request.method == 'POST':
        title_text = request.POST.get('title', '').strip()
        if title_text:
            Title.objects.create(title=title_text)
            return redirect('storygame:success')
    return render(request, 'storygame/create_title.html')


def success(request):
    """View to display a success message after title is saved."""
    return render(request, 'storygame/success.html')


def add_story_page(request, title_id=None):
    """View to add a new page to a story."""
    if title_id is None:
        # Show list of titles to choose from
        if request.method == 'POST':
            title_id = request.POST.get('title_id')
            if title_id:
                return redirect('storygame:add_story_page_with_id', title_id=title_id)

        titles = Title.objects.all()
        return render(request, 'storygame/select_title.html', {'titles': titles})

    # Get the title object
    title = get_object_or_404(Title, pk=title_id)

    if request.method == 'POST':
        content = request.POST.get('content', '').strip()
        if content:
            # Get the next page number
            last_page = StoryPage.objects.filter(title=title).order_by('-page_number').first()
            page_number = (last_page.page_number + 1) if last_page else 1

            StoryPage.objects.create(
                title=title,
                page_number=page_number,
                content=content
            )
            return redirect('storygame:page_success', title_id=title_id)

    return render(request, 'storygame/add_story_page.html', {'title': title})


def page_success(request, title_id):
    """View to display success message after story page is saved."""
    title = get_object_or_404(Title, pk=title_id)
    return render(request, 'storygame/page_success.html', {'title': title})


def story_list(request):
    """View to display all stories."""
    stories = Title.objects.all()
    return render(request, 'storygame/story_list.html', {'stories': stories})


def view_story(request, title_id):
    """View to display a complete story with all its pages."""
    title = get_object_or_404(Title, pk=title_id)
    pages = StoryPage.objects.filter(title=title).order_by('page_number')

    context = {
        'title': title,
        'pages': pages,
        'page_count': pages.count(),
    }
    return render(request, 'storygame/view_story.html', context)


def draw_on_page(request, page_id):
    """View to display a page and allow drawing on it."""
    page = get_object_or_404(StoryPage, pk=page_id)
    title = page.title

    if request.method == 'POST':
        drawing_data = request.POST.get('drawing_data', '')
        if drawing_data:
            page.drawing = drawing_data
            page.save()
            return redirect('storygame:draw_success', page_id=page_id)

    context = {
        'page': page,
        'title': title,
    }
    return render(request, 'storygame/draw_on_page.html', context)


def draw_success(request, page_id):
    """View to display success message after drawing is saved."""
    page = get_object_or_404(StoryPage, pk=page_id)
    context = {
        'page': page,
        'title': page.title,
    }
    return render(request, 'storygame/draw_success.html', context)


